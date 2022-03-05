#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from typing import List, Optional, Any, Dict, Callable

import torch
import torch.distributed as dist
from torchrec.distributed.dist_data import PooledEmbeddingsReduceScatter
from torchrec.distributed.embedding_lookup import GroupedPooledEmbeddingsLookup
from torchrec.distributed.embedding_sharding import (
    BaseEmbeddingLookup,
    SparseFeaturesAllToAll,
    BaseSparseFeaturesDist,
    bucketize_kjt_before_all2all,
)
from torchrec.distributed.embedding_types import (
    SparseFeatures,
    BaseGroupedFeatureProcessor,
)
from torchrec.distributed.sharding.rw_sharding import (
    BaseRwEmbeddingSharding,
)
from torchrec.distributed.sharding.vb_sharding import (
    BaseVariableBatchEmbeddingDist,
    VariableBatchShardingContext,
)
from torchrec.distributed.types import Awaitable


class VariableBatchRwSparseFeaturesDist(BaseSparseFeaturesDist[SparseFeatures]):
    """
    Bucketizes sparse features in RW fashion and then redistributes with an AlltoAll
    collective operation.
    Support variable batch size

    Constructor Args:
        pg (dist.ProcessGroup): ProcessGroup for AlltoAll communication.
        intra_pg (dist.ProcessGroup): ProcessGroup within single host group for AlltoAll
            communication.
        num_id_list_features (int): total number of id list features.
        num_id_score_list_features (int): total number of id score list features
        id_list_feature_hash_sizes (List[int]): hash sizes of id list features.
        id_score_list_feature_hash_sizes (List[int]): hash sizes of id score list features.
        device (Optional[torch.device]): device on which buffers will be allocated.
        has_feature_processor (bool): existence of feature processor (ie. position
            weighted features).
    """

    def __init__(
        self,
        # pyre-fixme[11]
        pg: dist.ProcessGroup,
        num_id_list_features: int,
        num_id_score_list_features: int,
        id_list_feature_hash_sizes: List[int],
        id_score_list_feature_hash_sizes: List[int],
        device: Optional[torch.device] = None,
        has_feature_processor: bool = False,
    ) -> None:
        super().__init__()
        self._world_size: int = pg.size()
        self._num_id_list_features = num_id_list_features
        self._num_id_score_list_features = num_id_score_list_features
        id_list_feature_block_sizes = [
            (hash_size + self._world_size - 1) // self._world_size
            for hash_size in id_list_feature_hash_sizes
        ]
        id_score_list_feature_block_sizes = [
            (hash_size + self._world_size - 1) // self._world_size
            for hash_size in id_score_list_feature_hash_sizes
        ]
        self.register_buffer(
            "_id_list_feature_block_sizes_tensor",
            torch.tensor(
                id_list_feature_block_sizes,
                device=device,
                dtype=torch.int32,
            ),
        )
        self.register_buffer(
            "_id_score_list_feature_block_sizes_tensor",
            torch.tensor(
                id_score_list_feature_block_sizes,
                device=device,
                dtype=torch.int32,
            ),
        )
        self._dist = SparseFeaturesAllToAll(
            pg=pg,
            id_list_features_per_rank=self._world_size * [self._num_id_list_features],
            id_score_list_features_per_rank=self._world_size
            * [self._num_id_score_list_features],
            device=device,
            variable_batch_size=True,
        )
        self._has_feature_processor = has_feature_processor
        self.unbucketize_permute_tensor: Optional[torch.Tensor] = None

    def forward(
        self,
        sparse_features: SparseFeatures,
    ) -> Awaitable[Awaitable[SparseFeatures]]:
        """
        Bucketizes sparse feature values into  world size number of buckets, and then
        performs AlltoAll operation.

        Call Args:
            sparse_features (SparseFeatures): sparse features to bucketize and
                redistribute.

        Returns:
            Awaitable[SparseFeatures]: awaitable of SparseFeatures.
        """

        if self._num_id_list_features > 0:
            assert sparse_features.id_list_features is not None
            (
                id_list_features,
                self.unbucketize_permute_tensor,
            ) = bucketize_kjt_before_all2all(
                sparse_features.id_list_features,
                num_buckets=self._world_size,
                block_sizes=self._id_list_feature_block_sizes_tensor,
                output_permute=False,
                bucketize_pos=self._has_feature_processor,
            )
        else:
            id_list_features = None

        if self._num_id_score_list_features > 0:
            assert sparse_features.id_score_list_features is not None
            id_score_list_features, _ = bucketize_kjt_before_all2all(
                sparse_features.id_score_list_features,
                num_buckets=self._world_size,
                block_sizes=self._id_score_list_feature_block_sizes_tensor,
                output_permute=False,
                bucketize_pos=False,
            )
        else:
            id_score_list_features = None

        bucketized_sparse_features = SparseFeatures(
            id_list_features=id_list_features,
            id_score_list_features=id_score_list_features,
        )
        return self._dist(bucketized_sparse_features)


class VariableBatchRwEmbeddingDistAwaitable(Awaitable[torch.Tensor]):
    def __init__(self, awaitable: Awaitable[torch.Tensor], batch_size: int) -> None:
        super().__init__()
        self._awaitable = awaitable
        self._batch_size = batch_size

    def _wait_impl(self) -> torch.Tensor:
        embedding = self._awaitable.wait()
        embedding = torch.narrow(embedding, 0, 0, self._batch_size)

        return embedding


class VariableBatchRwPooledEmbeddingDist(BaseVariableBatchEmbeddingDist[torch.Tensor]):
    def __init__(
        self,
        pg: dist.ProcessGroup,
    ) -> None:
        super().__init__()
        self._workers: int = pg.size()
        self._rank: int = pg.rank()
        self._dist = PooledEmbeddingsReduceScatter(pg)

    def forward(
        self,
        local_embs: torch.Tensor,
        sharding_ctx: VariableBatchShardingContext,
    ) -> Awaitable[torch.Tensor]:
        batch_size_per_rank_tensor = sharding_ctx.batch_size_per_rank_tensor
        batch_size_per_rank = sharding_ctx.batch_size_per_rank
        max_length = max(batch_size_per_rank)
        batch_size = batch_size_per_rank[self._rank]
        packed_pooled_embs, _ = torch.ops.fb.pack_segments(
            local_embs,
            lengths=batch_size_per_rank_tensor,
            max_length=max_length,
            pad_minf=False,  # Pad zeros in packed segments
        )
        awaitable_tensor = self._dist(
            packed_pooled_embs.view(self._workers * max_length, -1)
        )
        return VariableBatchRwEmbeddingDistAwaitable(awaitable_tensor, batch_size)


class VariableBatchRwPooledEmbeddingSharding(
    BaseRwEmbeddingSharding[SparseFeatures, torch.Tensor]
):
    """
    Shards pooled embeddings row-wise, i.e.. a given embedding table is entirely placed
    on a selected rank.
    Support Variable batch size.
    """

    def create_input_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseSparseFeaturesDist[SparseFeatures]:
        num_id_list_features = self._get_id_list_features_num()
        num_id_score_list_features = self._get_id_score_list_features_num()
        id_list_feature_hash_sizes = self._get_id_list_features_hash_sizes()
        id_score_list_feature_hash_sizes = self._get_id_score_list_features_hash_sizes()
        return VariableBatchRwSparseFeaturesDist(
            pg=self._pg,
            num_id_list_features=num_id_list_features,
            num_id_score_list_features=num_id_score_list_features,
            id_list_feature_hash_sizes=id_list_feature_hash_sizes,
            id_score_list_feature_hash_sizes=id_score_list_feature_hash_sizes,
            device=self._device,
            has_feature_processor=self._has_feature_processor,
        )

    def create_lookup(
        self,
        device: Optional[torch.device] = None,
        fused_params: Optional[Dict[str, Any]] = None,
        feature_processor: Optional[BaseGroupedFeatureProcessor] = None,
    ) -> BaseEmbeddingLookup:
        return GroupedPooledEmbeddingsLookup(
            grouped_configs=self._grouped_embedding_configs,
            grouped_score_configs=self._score_grouped_embedding_configs,
            fused_params=fused_params,
            pg=self._pg,
            device=device if device is not None else self._device,
            feature_processor=feature_processor,
        )

    def create_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> BaseVariableBatchEmbeddingDist[torch.Tensor]:
        return VariableBatchRwPooledEmbeddingDist(self._pg)