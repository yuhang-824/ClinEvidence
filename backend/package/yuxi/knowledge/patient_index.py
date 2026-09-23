"""患者文块的 Milvus 投影:写入、快照作用域混合检索、发布回读核对与孤儿向量清理。

患者共享 collection,按 embedding generation 管理;过滤条件一律由服务端从
Run 绑定的快照 manifest 构造,不接受模型或浏览器透传。检索为向量+BM25 双路
各自检索后应用层 RRF 融合:`score` 保持向量路余弦相似度口径(前端相似度展示
依赖该前提),最终顺序由 rrf_score 决定。Milvus 命中本身不能证明证据有效,
回读 PostgreSQL 才能引用。
"""

from __future__ import annotations

import asyncio

from yuxi.knowledge.rank_fusion import DEFAULT_RRF_K, reciprocal_rank_fusion
from yuxi.utils.logging_config import logger

PATIENT_COLLECTION_PREFIX = "patient_records"
PATIENT_SPARSE_FIELD = "content_sparse"
_DIM = 1024  # 默认向量维度;创建 collection 时以 embedding 配置为准
_CONTENT_ANALYZER_PARAMS = {"type": "chinese"}


def collection_name(generation: int = 1) -> str:
    """按索引代返回患者 collection 名称。"""
    return f"{PATIENT_COLLECTION_PREFIX}_g{generation}"


def _connect():
    """建立 pymilvus 连接;连接参数与通用知识库共用 MILVUS_URI/MILVUS_TOKEN 环境变量。"""
    import os

    from pymilvus import connections

    alias = "patient_index"
    if not connections.has_connection(alias):
        connections.connect(
            alias=alias,
            uri=os.getenv("MILVUS_URI", "http://localhost:19530"),
            token=os.getenv("MILVUS_TOKEN") or "",
        )
    return alias


def _build_patient_schema(embedding_dim: int):
    """构建含 BM25 稀疏投影的患者 collection schema。

    `content` 只服务于 Milvus 内置 BM25 的稀疏向量生成;文块内容真值在
    PostgreSQL,Milvus 内的副本不作为引用依据。
    """
    from pymilvus import CollectionSchema, DataType, FieldSchema, Function, FunctionType

    fields = [
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
        FieldSchema(name="patient_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="visit_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="document_version_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="document_revision_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="revision_version", dtype=DataType.INT64),
        FieldSchema(name="index_generation", dtype=DataType.INT64),
        FieldSchema(name="document_type", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=embedding_dim),
        FieldSchema(
            name="content",
            dtype=DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params=_CONTENT_ANALYZER_PARAMS,
        ),
        FieldSchema(name=PATIENT_SPARSE_FIELD, dtype=DataType.SPARSE_FLOAT_VECTOR),
    ]
    bm25_function = Function(
        name="content_bm25",
        input_field_names=["content"],
        output_field_names=[PATIENT_SPARSE_FIELD],
        function_type=FunctionType.BM25,
    )
    return CollectionSchema(
        fields,
        description="ClinEvidence patient record chunks",
        functions=[bm25_function],
    )


def _patient_collection_supports_hybrid(collection) -> bool:
    """检查患者 collection 是否具备 BM25 稀疏投影所需的 schema。"""
    from pymilvus import DataType, FunctionType

    fields = {field.name: field for field in collection.schema.fields}
    content_field = fields.get("content")
    sparse_field = fields.get(PATIENT_SPARSE_FIELD)
    if not content_field or content_field.dtype != DataType.VARCHAR:
        return False
    if (content_field.params or {}).get("enable_analyzer") is not True:
        return False
    if not sparse_field or sparse_field.dtype != DataType.SPARSE_FLOAT_VECTOR:
        return False

    return any(
        function.type == FunctionType.BM25
        and function.input_field_names == ["content"]
        and function.output_field_names == [PATIENT_SPARSE_FIELD]
        for function in collection.schema.functions
    )


def ensure_patient_collection(embedding_dim: int = _DIM, generation: int = 1):
    """创建或返回患者共享 collection。

    存量 collection 若缺 BM25 稀疏投影则重建(与文档知识库同策略):向量是
    PG 的派生数据,重建后检索返回空,需重新执行索引写入(重新发布)恢复。
    """
    from pymilvus import Collection, utility

    alias = _connect()
    name = collection_name(generation)
    if utility.has_collection(name, using=alias):
        collection = Collection(name, using=alias)
        if _patient_collection_supports_hybrid(collection):
            return collection
        logger.warning(
            f"Patient collection {name} schema does not support BM25, recreating; "
            "re-publish snapshots to restore the vector projection"
        )
        utility.drop_collection(name, using=alias)

    collection = Collection(name, schema=_build_patient_schema(embedding_dim), using=alias)
    collection.create_index(
        "embedding",
        {"index_type": "AUTOINDEX", "metric_type": "IP", "params": {}},
    )
    collection.create_index(
        PATIENT_SPARSE_FIELD,
        {
            "metric_type": "BM25",
            "index_type": "SPARSE_INVERTED_INDEX",
            "params": {"inverted_index_algo": "DAAT_MAXSCORE"},
        },
    )
    return collection


async def insert_patient_chunks(records: list[dict], generation: int = 1) -> int:
    """写入带患者/版本 metadata 的向量;records 至少含 chunk_id/patient_id/embedding/content。"""
    if not records:
        return 0
    collection = await asyncio.to_thread(ensure_patient_collection, len(records[0]["embedding"]), generation)

    def _insert() -> int:
        collection.insert(
            [
                [record["chunk_id"] for record in records],
                [record["patient_id"] for record in records],
                [record.get("visit_id") or "" for record in records],
                [record["document_id"] for record in records],
                [record["document_version_id"] for record in records],
                [record["document_revision_id"] for record in records],
                [int(record.get("revision_version") or 0) for record in records],
                [int(record.get("index_generation") or 1) for record in records],
                [record.get("document_type") or "" for record in records],
                [record["embedding"] for record in records],
                [record["content"] for record in records],
            ]
        )
        collection.flush()
        return len(records)

    return await asyncio.to_thread(_insert)


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "").replace('"', "") + '"'


def _snapshot_filter(patient_id: str, members: list[dict]) -> str:
    """构造快照作用域过滤表达式;逐成员匹配版本+解析修订+索引代,防止新修订混入旧快照。

    members 来自服务端快照 manifest,不含任何用户输入。
    """
    clauses = [
        f'(document_version_id == {_quote(member["document_version_id"])} '
        f'and document_revision_id == {_quote(member["document_revision_id"])} '
        f'and index_generation == {int(member.get("index_generation") or 1)})'
        for member in members
    ]
    return f'patient_id == {_quote(patient_id)} and ({" or ".join(clauses)})'


def _fuse_patient_hits(dense_hits: list[dict], sparse_hits: list[dict], k: int = DEFAULT_RRF_K) -> list[dict]:
    """RRF 融合双路命中并按融合分降序。

    `score` 只保留向量路余弦相似度(仅稠密路命中的文块携带);仅 BM25 命中的
    文块不伪造相似度,原始 BM25 分放入 `bm25_score`。
    """
    rrf_scores = reciprocal_rank_fusion(
        [
            [str(hit["chunk_id"]) for hit in dense_hits],
            [str(hit["chunk_id"]) for hit in sparse_hits],
        ],
        k=k,
    )

    merged: dict[str, dict] = {}
    for hit in dense_hits:
        key = str(hit["chunk_id"])
        hit["rrf_score"] = rrf_scores[key]
        merged[key] = hit
    for hit in sparse_hits:
        key = str(hit["chunk_id"])
        existing = merged.get(key)
        if existing is not None:
            existing["bm25_score"] = hit["score"]
            continue
        hit["bm25_score"] = hit.pop("score")
        hit["rrf_score"] = rrf_scores[key]
        merged[key] = hit
    return sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)


async def search_patient_chunks(
    *,
    patient_id: str,
    snapshot_members: list[dict],
    query_text: str,
    query_embedding: list[float],
    top_k: int = 8,
    generation: int = 1,
) -> list[dict]:
    """在当前 Run 快照的成员(版本+修订+索引代)内做向量+BM25 双路检索并 RRF 融合。

    融合后按 rrf_score 降序截断回 top_k,保持调用方工具契约的条数语义。
    """
    if not snapshot_members:
        return []
    collection = await asyncio.to_thread(ensure_patient_collection, len(query_embedding), generation)
    expr = _snapshot_filter(patient_id, snapshot_members)
    output_fields = ["chunk_id", "document_version_id", "document_type"]

    def _search_routes() -> tuple[list[dict], list[dict]]:
        collection.load()

        def _collect(route_results) -> list[dict]:
            hits = []
            for row in route_results:
                for hit in row:
                    hits.append(
                        {
                            "chunk_id": hit.entity.get("chunk_id") or hit.id,
                            "document_version_id": hit.entity.get("document_version_id"),
                            "document_type": hit.entity.get("document_type"),
                            "score": float(hit.score),
                        }
                    )
            return hits

        dense_hits = _collect(
            collection.search(
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "IP", "params": {"nprobe": 16}},
                limit=top_k,
                expr=expr,
                output_fields=output_fields,
            )
        )
        sparse_hits = _collect(
            collection.search(
                data=[query_text],
                anns_field=PATIENT_SPARSE_FIELD,
                param={"metric_type": "BM25", "params": {"drop_ratio_search": 0.0}},
                limit=top_k,
                expr=expr,
                output_fields=output_fields,
            )
        )
        return dense_hits, sparse_hits

    dense_hits, sparse_hits = await asyncio.to_thread(_search_routes)
    return _fuse_patient_hits(dense_hits, sparse_hits)[:top_k]


async def fetch_stored_chunk_ids(chunk_ids: list[str], generation: int = 1) -> set[str]:
    """按主键回读已写入的向量 ID,供发布前一致性核对。"""
    if not chunk_ids:
        return set()
    collection = await asyncio.to_thread(ensure_patient_collection, generation=generation)

    def _query() -> set[str]:
        collection.load()
        escaped = ",".join('"' + chunk_id.replace('\\', '').replace('"', '') + '"' for chunk_id in chunk_ids)
        rows = collection.query(expr=f"chunk_id in [{escaped}]", output_fields=["chunk_id"])
        return {row["chunk_id"] for row in rows}

    return await asyncio.to_thread(_query)


async def verify_vectors_for_publish(chunk_ids: list[str], generation: int = 1) -> None:
    """发布门禁:Milvus 中必须能回读全部预期 chunk_id,缺失即拒绝发布。

    允许在未配置 Milvus 的开发环境显式跳过:跳过必须通过环境变量
    YUXI_PATIENT_INDEX_SKIP_VERIFY=1 声明,并留下 warning 日志,不得静默成功。
    """
    import os

    if not chunk_ids:
        return
    if os.environ.get("YUXI_PATIENT_INDEX_SKIP_VERIFY") == "1":
        logger.warning(
            f"patient index vector verification skipped by explicit env flag; chunks={len(chunk_ids)}"
        )
        return
    stored = await fetch_stored_chunk_ids(chunk_ids, generation)
    missing = set(chunk_ids) - stored
    if missing:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail=f"向量投影核对失败:缺失 {len(missing)} 个文块向量,拒绝发布",
        )


async def delete_orphan_vectors(chunk_ids: list[str], generation: int = 1) -> int:
    """reconciliation:清理 PG 已不存在而 Milvus 残留的向量。"""
    if not chunk_ids:
        return 0
    collection = await asyncio.to_thread(ensure_patient_collection, generation=generation)

    def _delete() -> int:
        escaped = ",".join('"' + chunk_id.replace('\\', '').replace('"', '') + '"' for chunk_id in chunk_ids)
        collection.delete(expr=f"chunk_id in [{escaped}]")
        collection.flush()
        return len(chunk_ids)

    return await asyncio.to_thread(_delete)


__all__ = [
    "collection_name",
    "delete_orphan_vectors",
    "ensure_patient_collection",
    "fetch_stored_chunk_ids",
    "insert_patient_chunks",
    "search_patient_chunks",
    "verify_vectors_for_publish",
]
