"""患者索引混合检索的纯逻辑单测:schema 组装、BM25 支持判定与 RRF 融合。"""

import asyncio
import types

import pytest
from pymilvus import CollectionSchema, DataType, FieldSchema, FunctionType

from yuxi.knowledge.patient_index import (
    PATIENT_SPARSE_FIELD,
    _build_patient_schema,
    _fuse_patient_hits,
    _patient_collection_supports_hybrid,
    search_patient_chunks,
)


def _legacy_schema() -> CollectionSchema:
    """旧版患者 schema:只有稠密向量,没有 BM25 稀疏投影。"""
    return CollectionSchema(
        fields=[
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=4),
        ],
        description="legacy patient collection",
    )


def _collection_with(schema: CollectionSchema) -> types.SimpleNamespace:
    return types.SimpleNamespace(schema=schema)


def test_build_patient_schema_includes_bm25_projection():
    schema = _build_patient_schema(8)

    fields = {field.name: field for field in schema.fields}
    content_field = fields["content"]
    assert content_field.dtype == DataType.VARCHAR
    assert (content_field.params or {}).get("enable_analyzer") is True
    assert fields[PATIENT_SPARSE_FIELD].dtype == DataType.SPARSE_FLOAT_VECTOR
    assert any(
        function.type == FunctionType.BM25
        and function.input_field_names == ["content"]
        and function.output_field_names == [PATIENT_SPARSE_FIELD]
        for function in schema.functions
    )


def test_supports_hybrid_accepts_new_schema_and_rejects_legacy():
    assert _patient_collection_supports_hybrid(_collection_with(_build_patient_schema(8))) is True
    assert _patient_collection_supports_hybrid(_collection_with(_legacy_schema())) is False


def test_fuse_patient_hits_orders_by_consensus_and_keeps_similarity_scale():
    dense = [
        {"chunk_id": "chunk-a", "score": 0.92},
        {"chunk_id": "chunk-b", "score": 0.88},
    ]
    sparse = [
        {"chunk_id": "chunk-b", "score": 15.3},
        {"chunk_id": "chunk-c", "score": 9.1},
    ]

    fused = _fuse_patient_hits(dense, sparse)

    # 两路共识的 chunk-b 排第一,整体顺序由 rrf_score 决定
    assert [hit["chunk_id"] for hit in fused] == ["chunk-b", "chunk-a", "chunk-c"]
    # score 保留向量路余弦相似度口径,不写融合分
    assert fused[0]["score"] == 0.88
    assert fused[0]["bm25_score"] == 15.3
    assert fused[0]["rrf_score"] == pytest.approx(1 / 61 + 1 / 62)
    # 仅 BM25 命中的文块不伪造相似度,原始 BM25 分放进 bm25_score
    assert "score" not in fused[2]
    assert fused[2]["bm25_score"] == 9.1


def test_fuse_patient_hits_empty_routes_return_empty():
    assert _fuse_patient_hits([], []) == []


def test_search_patient_chunks_truncates_fused_hits_to_top_k(monkeypatch):
    """双路去重融合后的条数必须截断回 top_k,保持工具契约的条数语义。"""
    import yuxi.knowledge.patient_index as pi

    class _Entity:
        def __init__(self, chunk_id):
            self._data = {"chunk_id": chunk_id, "document_version_id": "v1", "document_type": "病理"}

        def get(self, key):
            return self._data.get(key)

    class _Hit:
        def __init__(self, chunk_id, score):
            self.entity = _Entity(chunk_id)
            self.id = chunk_id
            self.score = score

    class _FakeCollection:
        def load(self):
            pass

        def search(self, *, data, anns_field, limit, **kwargs):
            if anns_field == "embedding":
                return [[_Hit(f"dense-{i}", 0.9 - i * 0.1) for i in range(4)]]
            return [[_Hit(f"sparse-{i}", 5.0 - i) for i in range(4)]]

    monkeypatch.setattr(pi, "ensure_patient_collection", lambda *args, **kwargs: _FakeCollection())

    hits = asyncio.run(
        search_patient_chunks(
            patient_id="p1",
            snapshot_members=[{"document_version_id": "v", "document_revision_id": "r", "index_generation": 1}],
            query_text="病理",
            query_embedding=[0.1, 0.2],
            top_k=3,
        )
    )

    assert len(hits) == 3
    rrf_scores = [hit["rrf_score"] for hit in hits]
    assert rrf_scores == sorted(rrf_scores, reverse=True)
    assert all("rrf_score" in hit for hit in hits)
