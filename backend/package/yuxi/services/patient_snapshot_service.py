"""患者快照服务:解析修订审核、文块构建、快照原子发布。

发布事实全部在 PostgreSQL 事务内收敛;Milvus 只保存可重建投影,
写入成功而发布失败时新向量不可见(任何已发布 manifest 都不包含它们)。
"""

import hashlib
import json
import re
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


CHUNK_TARGET_CHARS = 800
CHUNK_HARD_CHARS = 1600
# 病历结构小标题:块按标题边界切分,保证每个小标题的内容完整(宁多切不切碎)
_HEADING_KEYWORDS = (
    "主诉",
    "病例特点",
    "现病史",
    "既往史",
    "个人史",
    "月经史",
    "婚育史",
    "家族史",
    "体格检查",
    "妇科检查",
    "专科检查",
    "辅助检查",
    "初步诊断",
    "入院诊断",
    "出院诊断",
    "诊疗计划",
    "诊疗经过",
    "治疗经过",
    "手术经过",
    "手术记录",
    "术后记录",
    "病程记录",
    "出院记录",
    "出院医嘱",
)
_DATE_HEAD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _is_heading_line(line: str) -> bool:
    """判断一行是否为病历结构小标题或病程条目时间头。"""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    if len(stripped) > 40:
        return False
    if _DATE_HEAD_RE.match(stripped):
        return True
    for keyword in _HEADING_KEYWORDS:
        if keyword in stripped and not stripped.endswith(("。", ";", ";", ".", ",")):
            return True
    return False


def _split_sections(text: str) -> list[dict]:
    """把内容切成 section;每个 section = 标题行 + 其下属内容,保留原文区间。"""
    sections: list[dict] = []
    current: dict = {"heading": None, "heading_start": None, "lines": []}
    cursor = 0
    for line in text.split("\n"):
        line_start = cursor
        line_end = min(line_start + len(line) + 1, len(text))  # 行区间含行尾换行;末行止于文本末尾
        cursor = line_start + len(line) + 1
        if _is_heading_line(line):
            if current["heading"] is not None or any(item[0].strip() for item in current["lines"]):
                sections.append(current)
            current = {"heading": line, "heading_start": line_start, "lines": []}
        else:
            current["lines"].append((line, line_start, line_end))
    sections.append(current)
    return sections


def build_chunks_for_revision(content: str) -> list[dict]:
    """结构感知分块:按病历小标题与病程时间头切 section,顺序组装成块。

    规则:一个块可含多个小标题,但每个小标题的正文必须完整(跨块超长 section
    按段落细分,且每个子块重复携带标题前缀);病程条目与其时间头绑定同块;
    宁可多切,不允许块内出现无标题的孤立正文。
    """
    text = content or ""
    chunks: list[dict] = []
    index = 0
    buffer: list[tuple[str, int, int]] = []
    buffer_heading: str | None = None

    def flush():
        nonlocal buffer, buffer_heading, index
        if not buffer:
            return
        content_text = "\n".join(item[0] for item in buffer).strip("\n")
        if content_text.strip():
            chunks.append(
                {
                    "index": index,
                    "content": content_text,
                    "char_start": buffer[0][1],
                    "char_end": buffer[-1][2],
                }
            )
            index += 1
        buffer = []
        buffer_heading = None

    for section in _split_sections(text):
        section_lines: list[tuple[str, int, int]] = []
        if section["heading"] is not None:
            heading_end = section["heading_start"] + len(section["heading"]) + 1
            section_lines.append((section["heading"], section["heading_start"], heading_end))
        section_lines.extend(section["lines"])
        section_len = sum(len(item[0]) + 1 for item in section_lines)

        if section_len > CHUNK_HARD_CHARS:
            flush()
            heading = (section["heading"] or "").strip()
            paragraph: list[tuple[str, int, int]] = []
            paragraph_len = 0
            parts: list[list[tuple[str, int, int]]] = []
            for item in section["lines"]:
                paragraph.append(item)
                paragraph_len += len(item[0]) + 1
                if paragraph_len >= CHUNK_TARGET_CHARS and item[0].strip() == "":
                    parts.append(paragraph)
                    paragraph, paragraph_len = [], 0
                elif paragraph_len >= CHUNK_HARD_CHARS:
                    parts.append(paragraph)
                    paragraph, paragraph_len = [], 0
            if paragraph:
                parts.append(paragraph)
            for part in parts:
                body = "\n".join(item[0] for item in part).strip("\n")
                if not body.strip():
                    continue
                chunk_content = f"{heading}\n{body}" if heading else body
                chunks.append(
                    {
                        "index": index,
                        "content": chunk_content,
                        "char_start": part[0][1],
                        "char_end": part[-1][2],
                    }
                )
                index += 1
            continue

        if buffer_len := sum(len(item[0]) + 1 for item in buffer):
            if buffer_len + section_len > CHUNK_TARGET_CHARS:
                flush()
        buffer.extend(section_lines)
        if sum(len(item[0]) + 1 for item in buffer) >= CHUNK_TARGET_CHARS:
            flush()
    flush()
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
    page_spans = _extract_page_spans(revision.structure_report)
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
                page_number=_resolve_page_number(page_spans, piece["char_start"], piece["char_end"]),
                document_type=document.document_type,
            )
        )
    revision.indexed_at = utc_now_naive()
    await db.commit()
    return {"revision_id": revision.id, "chunk_count": len(pieces), "status": "built"}


def _extract_page_spans(structure_report) -> list[dict]:
    """从解析结构报告提取页区间映射 [{start, end, page}];无报告或格式不符返回空。"""
    if not isinstance(structure_report, dict):
        return []
    spans = structure_report.get("page_spans")
    if not isinstance(spans, list):
        return []
    valid = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        start, end, page = span.get("start"), span.get("end"), span.get("page")
        if all(isinstance(value, int) for value in (start, end, page)) and end > start:
            valid.append({"start": start, "end": end, "page": page})
    return valid


def _resolve_page_number(page_spans: list[dict], char_start: int, char_end: int) -> int | None:
    """按块区间与页区间的最大重叠解析页码;解析报告缺失时返回 None。"""
    if not page_spans:
        return None
    best_page, best_overlap = None, 0
    for span in page_spans:
        overlap = min(char_end, span["end"]) - max(char_start, span["start"])
        if overlap > best_overlap:
            best_overlap = overlap
            best_page = span["page"]
    return best_page


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
                "content": chunk.content,
            }
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
    )
    return {"revision_id": revision.id, "vector_count": written}


async def finalize_batch(*, batch_id: str, current_uid: str, db: AsyncSession) -> dict:
    """Owner 一键收口:审核 → 建块 → 写向量 → 发布快照。

    每一步幂等(已审核/已建块/已写入的跳过),整体可安全重试;
    仅服务 review_required 状态的批次,最终事实由 publish_snapshot 原子收敛。
    """
    from fastapi import HTTPException as _HTTPException

    import_repo = PatientImportRepository(db)
    batch = await import_repo.lock_batch(str(batch_id))
    if batch is None:
        raise _HTTPException(status_code=404, detail="导入批次不存在")
    patient = await PatientRepository(db).get_accessible(batch.patient_id, str(current_uid))
    if patient is None:
        raise _HTTPException(status_code=404, detail="导入批次不存在")
    if patient.owner_uid != str(current_uid):
        raise _HTTPException(status_code=403, detail="仅患者 Owner 可执行审核发布")
    if batch.status != "review_required":
        raise _HTTPException(status_code=409, detail=f"批次状态 {batch.status} 不可执行审核发布")

    versions = (
        await db.execute(
            select(PatientDocumentVersion).where(PatientDocumentVersion.import_batch_id == batch.id)
        )
    ).scalars().all()
    if not versions:
        raise _HTTPException(status_code=409, detail="批次没有原件版本")

    steps = []
    for version in versions:
        revisions = (
            await db.execute(
                select(PatientDocumentRevision).where(
                    PatientDocumentRevision.document_version_id == version.id
                )
            )
        ).scalars().all()
        for revision in revisions:
            if revision.status == "review_required":
                await approve_revision(revision_id=revision.id, current_uid=current_uid, db=db)
                steps.append(f"approved:{revision.id[:8]}")
            built = await build_and_store_chunks(revision_id=revision.id, current_uid=current_uid, db=db)
            if built["status"] == "built":
                steps.append(f"chunks:{revision.id[:8]}:{built['chunk_count']}")
            indexed = await index_revision_chunks(revision_id=revision.id, current_uid=current_uid, db=db)
            if indexed.get("vector_count"):
                steps.append(f"indexed:{revision.id[:8]}:{indexed['vector_count']}")

    published = await publish_snapshot(batch_id=batch.id, current_uid=current_uid, db=db)
    return {"batch": published, "steps": steps}
