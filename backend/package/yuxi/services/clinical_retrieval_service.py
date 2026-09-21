"""临床双通道检索服务:作用域解析、患者快照内检索、证据回读与引用验证。

作用域事实:患者与快照一律由服务端从会话与 Run 解析;模型传入的患者标识
不参与过滤。检索命中必须回读 PostgreSQL 并再次校验快照成员后才能作为证据。
"""

import os

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.storage.postgres.models_clinical import (
    Patient,
    PatientChunk,
    PatientDocumentRevision,
    PatientSnapshot,
    PatientSnapshotMember,
)

PATIENT_EMBEDDING_SPEC_ENV = "YUXI_PATIENT_EMBEDDING_SPEC"


async def resolve_patient_scope(
    db: AsyncSession, *, thread_id: str, uid: str, snapshot_id: str | None = None
) -> dict:
    """从会话解析患者与 Run 作用域快照;不接受模型透传的患者或快照标识。

    快照缺省取患者当前已发布快照;显式传入的 snapshot_id 必须属于该患者。
    """
    conversation = await ConversationRepository(db).get_conversation_by_thread_id(thread_id)
    if conversation is None or conversation.uid != str(uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")
    if not conversation.patient_id:
        raise HTTPException(status_code=400, detail="会话未绑定患者")
    patient = await db.get(Patient, conversation.patient_id)
    if patient is None or patient.status == "deleted":
        raise HTTPException(status_code=404, detail="患者不存在")

    resolved_snapshot_id = snapshot_id or patient.current_snapshot_id
    if resolved_snapshot_id is None:
        raise HTTPException(status_code=409, detail="患者尚无已发布快照,不能执行患者检索")
    snapshot = await db.get(PatientSnapshot, resolved_snapshot_id)
    if snapshot is None or snapshot.patient_id != patient.id or snapshot.status != "published":
        raise HTTPException(status_code=409, detail="请求的快照不属于当前患者或未发布")
    return {"patient": patient, "snapshot": snapshot, "thread_id": thread_id}


async def snapshot_member_tuples(db: AsyncSession, snapshot: PatientSnapshot) -> list[dict]:
    """返回快照 manifest 成员(版本、修订 UUID、修订号、索引代);过滤与引用验证共用。"""
    members = (
        await db.execute(
            select(PatientSnapshotMember).where(PatientSnapshotMember.snapshot_id == snapshot.id)
        )
    ).scalars().all()
    result = []
    for member in members:
        revision = await db.get(PatientDocumentRevision, member.document_revision_id)
        result.append(
            {
                "document_version_id": member.document_version_id,
                "document_revision_id": member.document_revision_id,
                "revision_version": revision.version if revision else None,
                "index_generation": member.index_generation,
            }
        )
    return result


async def get_patient_context_view(db: AsyncSession, *, thread_id: str, uid: str) -> dict:
    """read_patient_context:当前患者的脱敏摘要、就诊与本次快照信息。"""
    from yuxi.repositories.patient_repository import PatientRepository
    from yuxi.services.patient_service import serialize_patient_summary

    scope = await resolve_patient_scope(db, thread_id=thread_id, uid=uid)
    patient = scope["patient"]
    encounters = [
        {
            "id": item.id,
            "encounter_type": item.encounter_type,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "ended_at": item.ended_at.isoformat() if item.ended_at else None,
        }
        for item in await PatientRepository(db).list_encounters(patient.id)
    ]
    summary = serialize_patient_summary(patient)
    summary["snapshot_id"] = scope["snapshot"].id
    summary["snapshot_sequence"] = scope["snapshot"].sequence
    return {"patient": summary, "encounters": encounters}


async def _embed_query(query_text: str) -> list[float]:
    """按部署配置的 embedding 规格编码查询;写入与查询共用同一模型。"""
    spec = os.environ.get(PATIENT_EMBEDDING_SPEC_ENV, "")
    if not spec:
        raise HTTPException(
            status_code=409,
            detail=f"未配置患者向量模型({PATIENT_EMBEDDING_SPEC_ENV}),患者检索不可用",
        )
    from yuxi.models.embed import select_embedding_model

    model = select_embedding_model(spec)
    vectors = await model.abatch_encode([query_text], batch_size=1)
    return list(vectors[0])


async def search_patient_records_view(
    db: AsyncSession,
    *,
    thread_id: str,
    uid: str,
    query_text: str,
    top_k: int = 8,
    visit_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    document_types: list[str] | None = None,
) -> list[dict]:
    """search_patient_records:仅在当前 Run 快照成员内检索,命中回读 PG 后返回。"""
    normalized_query = str(query_text or "").strip()
    if not normalized_query:
        return []
    scope = await resolve_patient_scope(db, thread_id=thread_id, uid=uid)
    members = await snapshot_member_tuples(db, scope["snapshot"])

    from yuxi.knowledge.patient_index import search_patient_chunks

    embedding = await _embed_query(normalized_query)
    hits = await search_patient_chunks(
        patient_id=scope["patient"].id,
        snapshot_members=members,
        query_embedding=embedding,
        top_k=top_k,
    )
    return await _hydrate_chunks(db, scope, hits, document_types=document_types)


async def read_patient_evidence_view(
    db: AsyncSession, *, thread_id: str, uid: str, chunk_id: str
) -> dict:
    """read_evidence_excerpt:回读原文窗口,并验证证据属于当前 Run 快照。"""
    scope = await resolve_patient_scope(db, thread_id=thread_id, uid=uid)
    hydrated = await _hydrate_chunks(
        db, scope, [{"chunk_id": str(chunk_id), "score": None}], document_types=None
    )
    if not hydrated:
        raise HTTPException(status_code=404, detail="证据不存在或不属于当前患者快照")
    return hydrated[0]


async def verify_patient_citation(
    db: AsyncSession, *, patient_id: str, snapshot_id: str, cited_chunk_ids: list[str]
) -> dict:
    """引用验证:回答保存前核对每个患者引用都落在当前快照成员内。

    伪造或跨快照引用必须被拒绝,不允许静默丢弃。
    """
    snapshot = await db.get(PatientSnapshot, snapshot_id)
    if snapshot is None or snapshot.patient_id != patient_id or snapshot.status != "published":
        raise HTTPException(status_code=409, detail="引用校验基准快照无效")
    member_chunk_ids = set(
        (
            await db.execute(
                select(PatientChunk.chunk_id).where(
                    PatientChunk.document_version_id.in_(
                        select(PatientSnapshotMember.document_version_id).where(
                            PatientSnapshotMember.snapshot_id == snapshot.id
                        )
                    )
                )
            )
        ).scalars()
    )
    rejected = [chunk_id for chunk_id in cited_chunk_ids if chunk_id not in member_chunk_ids]
    return {
        "accepted": [chunk_id for chunk_id in cited_chunk_ids if chunk_id in member_chunk_ids],
        "rejected": rejected,
    }


async def _hydrate_chunks(db: AsyncSession, scope: dict, hits: list[dict], *, document_types) -> list[dict]:
    """按 chunk_id 回读 PG 内容并再次校验患者、版本与修订成员;越界命中丢弃并告警。"""
    from yuxi.utils.logging_config import logger

    member_keys = {
        (member["document_version_id"], member["revision_version"])
        for member in await snapshot_member_tuples(db, scope["snapshot"])
    }
    hydrated: list[dict] = []
    for hit in hits:
        chunk = await db.get(PatientChunk, hit["chunk_id"])
        if chunk is None or chunk.patient_id != scope["patient"].id:
            logger.warning(f"patient retrieval dropped cross-patient hit: {hit['chunk_id']}")
            continue
        if (chunk.document_version_id, chunk.revision_version) not in member_keys:
            logger.warning(f"patient retrieval dropped out-of-snapshot hit: {hit['chunk_id']}")
            continue
        if document_types and chunk.document_type not in document_types:
            continue
        hydrated.append(
            {
                "chunk_id": chunk.chunk_id,
                "content": chunk.content,
                "document_id": chunk.document_id,
                "document_version_id": chunk.document_version_id,
                "document_type": chunk.document_type,
                "page_number": chunk.page_number,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "score": hit.get("score"),
                "patient_id": chunk.patient_id,
                "snapshot_id": scope["snapshot"].id,
                "snapshot_sequence": scope["snapshot"].sequence,
            }
        )
    return hydrated
