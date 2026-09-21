"""患者快照服务:解析修订审核、文块构建、快照原子发布。

发布事实全部在 PostgreSQL 事务内收敛;Milvus 只保存可重建投影,
写入成功而发布失败时新向量不可见(任何已发布 manifest 都不包含它们)。
"""

import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.patient_import_repository import PatientImportRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.storage.postgres.models_clinical import (
    Patient,
    PatientChunk,
    PatientDocument,
    PatientDocumentRevision,
    PatientDocumentVersion,
    PatientImportBatch,
    PatientSnapshot,
    PatientSnapshotMember,
)
from yuxi.utils.datetime_utils import utc_now_naive

CHUNK_TARGET_CHARS = 800


async def approve_revision(*, revision_id: str, current_uid: str, db: AsyncSession) -> dict:
    """人工内容审核:review_required → approved;审核后修订不可变。"""
    revision = await db.scalar(select(PatientDocumentRevision).where(PatientDocumentRevision.id == revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="解析修订不存在")
    if revision.status != "review_required":
        raise HTTPException(status_code=409, detail=f"修订状态 {revision.status} 不接受审核")
    await _require_patient_owner_for_revision(db, revision, current_uid)
    revision.status = "approved"
    revision.approved_by = str(current_uid)
    revision.approved_at = utc_now_naive()
    await db.commit()
    return {"id": revision.id, "status": revision.status}


def build_chunks_for_revision(content: str) -> list[dict]:
    """确定性字符窗口分块;优先在换行处切分,保留字符区间供引用定位。"""
    text = content or ""
    chunks: list[dict] = []
    start = 0
    index = 0
    while start < len(text):
        end = min(start + CHUNK_TARGET_CHARS, len(text))
        if end < len(text):
            newline = text.rfind("\n", start + CHUNK_TARGET_CHARS // 2, end)
            if newline > start:
                end = newline + 1
        chunks.append({"index": index, "content": text[start:end], "char_start": start, "char_end": end})
        index += 1
        start = end
    return chunks


async def build_and_store_chunks(*, revision_id: str, current_uid: str, db: AsyncSession) -> dict:
    """为已审核修订构建 patient_chunks;同一修订重复构建幂等返回。"""
    revision = await db.scalar(select(PatientDocumentRevision).where(PatientDocumentRevision.id == revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="解析修订不存在")
    if revision.status != "approved":
        raise HTTPException(status_code=409, detail="仅已审核修订可构建文块")
    await _require_patient_owner_for_revision(db, revision, current_uid)
    existing = await db.scalar(
        select(PatientChunk.chunk_id).where(
            PatientChunk.document_version_id == revision.document_version_id,
            PatientChunk.revision_version == revision.version,
        )
    )
    if existing is not None:
        return {"revision_id": revision.id, "chunk_count": None, "status": "already_built"}

    version = await db.get(PatientDocumentVersion, revision.document_version_id)
    document = await db.get(PatientDocument, version.document_id)
    pieces = build_chunks_for_revision(revision.content or "")
    for piece in pieces:
        db.add(
            PatientChunk(
                chunk_id=str(uuid.uuid4()),
                patient_id=version.patient_id,
                visit_id=document.visit_id,
                document_id=document.id,
                document_version_id=version.id,
                revision_version=revision.version,
                chunk_index=piece["index"],
                content=piece["content"],
                char_start=piece["char_start"],
                char_end=piece["char_end"],
                page_number=None,
                document_type=document.document_type,
            )
        )
    revision.indexed_at = utc_now_naive()
    await db.commit()
    return {"revision_id": revision.id, "chunk_count": len(pieces), "status": "built"}


async def publish_snapshot(*, batch_id: str, current_uid: str, db: AsyncSession) -> dict:
    """原子发布患者快照:成员与投影核对通过后,单事务内建立快照并推进批次终态。

    发布前置:批次通过门禁、全部修订 approved 且文块已构建;Milvus 投影回读由
    patient_index 执行,核对失败时发布被拒绝,PG 事实保持不变。
    """
    import_repo = PatientImportRepository(db)
    batch = await import_repo.lock_batch(str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    patient = await PatientRepository(db).get_accessible(batch.patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    if patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可发布快照")
    if batch.status == "published":
        result = PatientImportRepository.serialize_batch(batch)
        result.update({"snapshot_id": batch.published_snapshot_id, "already_published": True})
        return result
    if batch.status != "review_required":
        raise HTTPException(status_code=409, detail=f"批次状态 {batch.status} 不可发布")

    members = await _collect_members_for_batch(db, batch)
    if not members:
        raise HTTPException(status_code=409, detail="批次没有已审核且已建块的修订,不能发布")

    chunk_ids = [chunk.chunk_id for _version, _revision, chunks in members for chunk in chunks]
    from yuxi.knowledge.patient_index import verify_vectors_for_publish

    await verify_vectors_for_publish(chunk_ids)

    snapshot = await _create_snapshot_locked(db, patient_id=batch.patient_id, members=members, batch=batch)
    batch.published_snapshot_id = snapshot.id
    await import_repo.transition_batch(batch, "verifying")
    await import_repo.transition_batch(batch, "published")
    patient.current_snapshot_id = snapshot.id
    await db.commit()
    result = PatientImportRepository.serialize_batch(batch)
    result.update({"snapshot_id": snapshot.id, "already_published": False, "member_count": len(members)})
    return result


async def _collect_members_for_batch(db: AsyncSession, batch: PatientImportBatch):
    """收集批次名下 approved 修订,返回 (version, revision, chunks);缺块即拒绝发布。"""
    versions = (
        await db.execute(
            select(PatientDocumentVersion).where(PatientDocumentVersion.import_batch_id == batch.id)
        )
    ).scalars().all()
    members = []
    for version in versions:
        revision = (
            await db.execute(
                select(PatientDocumentRevision)
                .where(
                    PatientDocumentRevision.document_version_id == version.id,
                    PatientDocumentRevision.status == "approved",
                )
                .order_by(PatientDocumentRevision.version.desc())
            )
        ).scalars().first()
        if revision is None:
            continue
        chunks = (
            await db.execute(
                select(PatientChunk)
                .where(
                    PatientChunk.document_version_id == version.id,
                    PatientChunk.revision_version == revision.version,
                )
                .order_by(PatientChunk.chunk_index)
            )
        ).scalars().all()
        if not chunks:
            raise HTTPException(status_code=409, detail=f"版本 {version.id} 尚未构建文块,不能发布")
        members.append((version, revision, chunks))
    return members


async def _create_snapshot_locked(db: AsyncSession, *, patient_id: str, members, batch: PatientImportBatch):
    """锁定患者行后创建下一序号快照:复制上一快照成员并替换同逻辑文档版本。"""
    patient = await db.scalar(select(Patient).where(Patient.id == str(patient_id)).with_for_update())
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")

    latest = (
        await db.execute(
            select(PatientSnapshot)
            .where(PatientSnapshot.patient_id == str(patient_id))
            .order_by(PatientSnapshot.sequence.desc())
        )
    ).scalars().first()

    selected: dict[str, dict] = {}
    if latest is not None:
        prior_members = (
            await db.execute(select(PatientSnapshotMember).where(PatientSnapshotMember.snapshot_id == latest.id))
        ).scalars().all()
        for member in prior_members:
            selected[member.document_id] = {
                "document_version_id": member.document_version_id,
                "document_revision_id": member.document_revision_id,
                "index_generation": member.index_generation,
            }
        latest.status = "superseded"

    for version, revision, _chunks in members:
        selected[version.document_id] = {
            "document_version_id": version.id,
            "document_revision_id": revision.id,
            "index_generation": 1,
        }

    member_rows = [
        {"document_id": document_id, **fields}
        for document_id, fields in sorted(selected.items())
    ]
    manifest_hash = hashlib.sha256(
        json.dumps(member_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot = PatientSnapshot(
        id=str(uuid.uuid4()),
        patient_id=str(patient_id),
        sequence=(latest.sequence + 1) if latest is not None else 1,
        status="published",
        manifest_hash=manifest_hash,
        created_by_batch_id=batch.id,
        published_at=utc_now_naive(),
    )
    db.add(snapshot)
    await db.flush()
    for row in member_rows:
        db.add(PatientSnapshotMember(snapshot_id=snapshot.id, **row))
    return snapshot


async def _require_patient_owner_for_revision(db: AsyncSession, revision, current_uid: str) -> None:
    """审核与建块操作要求操作者是患者 Owner。"""
    version = await db.get(PatientDocumentVersion, revision.document_version_id)
    patient = await db.get(Patient, version.patient_id)
    if patient is None or patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可执行该操作")


async def index_revision_chunks(*, revision_id: str, current_uid: str, db: AsyncSession) -> dict:
    """将已建块修订的文块编码并写入患者向量投影;与查询共用同一 embedding 配置。"""
    revision = await db.scalar(select(PatientDocumentRevision).where(PatientDocumentRevision.id == revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="解析修订不存在")
    await _require_patient_owner_for_revision(db, revision, current_uid)
    chunks = (
        await db.execute(
            select(PatientChunk)
            .where(
                PatientChunk.document_version_id == revision.document_version_id,
                PatientChunk.revision_version == revision.version,
            )
            .order_by(PatientChunk.chunk_index)
        )
    ).scalars().all()
    if not chunks:
        raise HTTPException(status_code=409, detail="修订尚未构建文块,不能写入投影")

    from yuxi.services.clinical_retrieval_service import _embed_query

    embeddings = [await _embed_query(chunk.content) for chunk in chunks]
    from yuxi.knowledge.patient_index import insert_patient_chunks

    written = await insert_patient_chunks(
        [
            {
                "chunk_id": chunk.chunk_id,
                "patient_id": chunk.patient_id,
                "visit_id": chunk.visit_id,
                "document_id": chunk.document_id,
                "document_version_id": chunk.document_version_id,
                "document_revision_id": revision.id,
                "revision_version": chunk.revision_version,
                "index_generation": 1,
                "document_type": chunk.document_type,
                "embedding": embedding,
            }
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
    )
    return {"revision_id": revision.id, "vector_count": written}
