"""RRF 倒数排名融合的纯逻辑单测。"""

import pytest

from yuxi.knowledge.rank_fusion import reciprocal_rank_fusion


def test_consensus_across_routes_outranks_single_route_top():
    """两路共识的键胜过只在单路靠前的键,分数按 k+名次 累加。"""
    scores = reciprocal_rank_fusion([["a", "b", "c"], ["b", "d"]])

    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 61 + 1 / 62)
    assert scores["c"] == pytest.approx(1 / 63)
    assert scores["d"] == pytest.approx(1 / 62)
    assert max(scores, key=scores.get) == "b"


def test_rank_starts_at_one_per_route():
    """名次从 1 起:第 1 名贡献 1/(k+1),rank 从 0 起的实现会给出 1/k。"""
    scores = reciprocal_rank_fusion([["only"]])
    assert scores["only"] == pytest.approx(1 / 61)


def test_k_controls_head_dominance():
    """k 越小头部名次优势越大。"""
    sharp = reciprocal_rank_fusion([["a", "b"]], k=1)
    flat = reciprocal_rank_fusion([["a", "b"]], k=1000)

    assert sharp["a"] - sharp["b"] > flat["a"] - flat["b"]


def test_rejects_non_positive_k():
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=0)
