"""患者文块的 Milvus 投影:写入、快照作用域检索、发布回读核对与孤儿向量清理。

患者共享 collection,按 embedding generation 管理;过滤条件一律由服务端从
Run 绑定的快照 manifest 构造,不接受模型或浏览器透传。Milvus 命中本身不能
证明证据有效,回读 PostgreSQL 才能引用。
"""

from __future__ import annotations

import asyncio

from yuxi.utils.logging_config import logger

PATIENT_COLLECTION_PREFIX = "patient_records"
_DIM = 1024  # 默认向量维度;创建 collection 时以 embedding 配置为准


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


def ensure_patient_collection(embedding_dim: int = _DIM, generation: int = 1):
    """创建或返回患者共享 collection;字段与计划 7.1 一致。"""
    from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, utility

    alias = _connect()
    name = collection_name(generation)
    if utility.has_collection(name, using=alias):
        return Collection(name, using=alias)

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
    ]
    schema = CollectionSchema(fields, description="ClinEvidence patient record chunks")
    collection = Collection(name, schema, using=alias)
    collection.create_index(
        "embedding",
        {"index_type": "AUTOINDEX", "metric_type": "IP", "params": {}},
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


async def search_patient_chunks(
    *,
    patient_id: str,
    snapshot_members: list[dict],
    query_embedding: list[float],
    top_k: int = 8,
    generation: int = 1,
) -> list[dict]:
    """在当前 Run 快照的成员(版本+修订+索引代)内检索;返回命中 chunk_id 与分数。"""
    if not snapshot_members:
        return []
    collection = await asyncio.to_thread(ensure_patient_collection, len(query_embedding), generation)

    def _search() -> list[dict]:
        collection.load()
        results = collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "IP", "params": {"nprobe": 16}},
            limit=top_k,
            expr=_snapshot_filter(patient_id, snapshot_members),
            output_fields=["chunk_id", "document_version_id", "document_type"],
        )
        hits = []
        for row in results:
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

    return await asyncio.to_thread(_search)


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
