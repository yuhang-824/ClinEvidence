"""倒数排名融合(RRF):把多路检索的名次融合为统一排序分数。

RRF 只消费各路内部的排名,不比较分数大小,因此对向量相似度与 BM25 分数
的量纲差异免疫。各路分数仍负责决定自己路内的名次;融合分只表达多路共识
强度,不是相似度,不能用于绝对质量门控或向用户展示为"相似度"。
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
) -> dict[str, float]:
    """按 RRF 公式融合多路排名,返回每个元素的融合分。

    rankings 是各路按相关度降序排列的键列表;同一键出现在多路时贡献累加,
    只出现在部分路的键只累加出现的路。k 控制头部名次的优势衰减,k 越小
    头部越主导。返回值未排序,由调用方按需要排序。
    """
    if k <= 0:
        raise ValueError(f"RRF k 必须为正数,收到 {k}")
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


__all__ = ["DEFAULT_RRF_K", "reciprocal_rank_fusion"]
