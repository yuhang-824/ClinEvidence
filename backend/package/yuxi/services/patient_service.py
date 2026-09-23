"""患者域用例服务:创建、查询、脱敏编号与归档、就诊管理。

权限边界:可见性最终在 repository 查询执行;服务层只编排用例与校验。
patients 行的身份字段(owner_uid、identity_fingerprint)与绑定关系不提供任何更新入口。
"""

import secrets

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.patient_repository import PatientRepository, new_clinical_id
from yuxi.storage.postgres.models_clinical import PATIENT_CATEGORY_PREFIXES, Encounter, Patient

_DISPLAY_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_MAX_DISPLAY_CODE_ATTEMPTS = 8


def infer_patient_category(display_code: str | None) -> str | None:
    """只从明确包含癌种名称的脱敏编号推断分类。"""
    normalized_code = str(display_code or "").strip()
    return next((category for category in PATIENT_CATEGORY_PREFIXES if normalized_code.startswith(category)), None)


def normalize_patient_category(category: str) -> str:
    """规范化病种名称并拒绝空白或过长输入。"""
    normalized = category.strip()
    if not normalized or len(normalized) > 32:
        raise HTTPException(status_code=400, detail="患者病种不能为空且最多 32 字")
    return normalized


async def _generate_display_code(repo: PatientRepository) -> str:
    """生成全局唯一脱敏编号;去除易混字符,冲突时重试。"""
    for _ in range(_MAX_DISPLAY_CODE_ATTEMPTS):
        code = "P-" + "".join(secrets.choice(_DISPLAY_CODE_ALPHABET) for _ in range(8))
        if not await repo.display_code_exists(code):
            return code
    raise HTTPException(status_code=500, detail="脱敏编号生成冲突,请重试")


async def create_patient_view(
    *,
    current_uid: str,
    display_code: str | None,
    category: str | None = None,
    identity_fingerprint: str | None = None,
    db: AsyncSession,
) -> dict:
    """创建患者;创建者即 Owner。"""
    category = normalize_patient_category(category) if category is not None else infer_patient_category(display_code)
    repo = PatientRepository(db)
    normalized_code = str(display_code or "").strip() or None
    if normalized_code:
        if await repo.display_code_exists(normalized_code):
            raise HTTPException(status_code=409, detail="脱敏编号已存在")
    else:
        normalized_code = await _generate_display_code(repo)
    patient = Patient(
        id=new_clinical_id(),
        owner_uid=str(current_uid),
        display_code=normalized_code,
        category=category,
        status="active",
        identity_fingerprint=identity_fingerprint,
    )
    await repo.add(patient)
    await db.commit()
    return patient.to_dict()


async def list_patients_view(*, current_uid: str, db: AsyncSession) -> list[dict]:
    """列出当前用户可访问的患者。"""
    patients = await PatientRepository(db).list_accessible(str(current_uid))
    return [patient.to_dict() for patient in patients]


async def get_patient_view(*, patient_id: str, current_uid: str, db: AsyncSession) -> dict:
    """读取单个可访问患者;不可见与不存在同形返回 404。"""
    patient = await PatientRepository(db).get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return patient.to_dict()


async def update_patient_view(
    *,
    patient_id: str,
    current_uid: str,
    display_code: str | None = None,
    category: str | None = None,
    status: str | None = None,
    db: AsyncSession,
) -> dict:
    """更新脱敏编号、分类或归档状态;仅 Owner 可操作,身份字段不可触碰。"""
    repo = PatientRepository(db)
    patient = await repo.get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可维护患者资料")
    if status is not None and status not in {"active", "archived"}:
        raise HTTPException(status_code=400, detail="status 仅支持 active/archived;删除走独立清理流程")
    if category is not None:
        category = normalize_patient_category(category)
    if display_code is not None:
        normalized_code = str(display_code).strip()
        if not normalized_code:
            raise HTTPException(status_code=400, detail="脱敏编号不能为空")
        if normalized_code != patient.display_code and await repo.display_code_exists(normalized_code):
            raise HTTPException(status_code=409, detail="脱敏编号已存在")
        patient.display_code = normalized_code
    if status is not None:
        patient.status = status
    if category is not None:
        patient.category = category
    await db.commit()
    return patient.to_dict()


async def create_encounter_view(
    *,
    patient_id: str,
    current_uid: str,
    encounter_type: str,
    external_visit_key: str | None = None,
    started_at=None,
    ended_at=None,
    db: AsyncSession,
) -> dict:
    """为患者创建就诊;就诊归属必须有原文依据。"""
    if encounter_type not in {"inpatient", "outpatient", "other"}:
        raise HTTPException(status_code=400, detail="encounter_type 仅支持 inpatient/outpatient/other")
    repo = PatientRepository(db)
    patient = await repo.get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    normalized_key = str(external_visit_key or "").strip() or None
    encounter = Encounter(
        id=new_clinical_id(),
        patient_id=patient.id,
        external_visit_key=normalized_key,
        encounter_type=encounter_type,
        started_at=started_at,
        ended_at=ended_at,
        status="active",
    )
    try:
        await repo.add_encounter(encounter)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if "uq_encounters_patient_visit_key" in str(exc):
            raise HTTPException(status_code=409, detail="该就诊号已存在") from exc
        raise
    return _serialize_encounter(encounter)


async def list_encounters_view(*, patient_id: str, current_uid: str, db: AsyncSession) -> list[dict]:
    """列出患者就诊;不可见患者与不存在同形返回 404。"""
    repo = PatientRepository(db)
    patient = await repo.get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return [_serialize_encounter(item) for item in await repo.list_encounters(patient.id)]


async def require_patient_for_thread_binding(
    *,
    agent,
    patient_id: str | None,
    current_uid: str,
    db: AsyncSession,
) -> Patient | None:
    """会话创建时解析并校验患者绑定;返回锁定的患者行,普通会话返回 None。

    诊疗 Agent(requires_patient)必须提供 patient_id;提供时按可见性锁定 active 患者。
    """
    config_json = getattr(agent, "config_json", None) or {}
    context_config = config_json.get("context") or {}
    requires_patient = bool(
        context_config.get("requires_patient") or config_json.get("requires_patient")
    )
    normalized_id = str(patient_id or "").strip() or None
    if requires_patient and normalized_id is None:
        raise HTTPException(status_code=400, detail="该智能体要求绑定患者")
    if normalized_id is None:
        return None
    patient = await PatientRepository(db).lock_accessible(normalized_id, str(current_uid))
    if patient is None or patient.status != "active":
        raise HTTPException(status_code=404, detail="患者不存在")
    return patient


def serialize_patient_summary(patient: Patient | None) -> dict | None:
    """会话响应中的患者摘要;只含脱敏字段。"""
    if patient is None:
        return None
    return {
        "id": patient.id,
        "display_code": patient.display_code,
        "status": patient.status,
        "current_snapshot_id": patient.current_snapshot_id,
    }


def _serialize_encounter(encounter: Encounter) -> dict:
    """序列化就诊公开字段。"""
    return {
        "id": encounter.id,
        "patient_id": encounter.patient_id,
        "external_visit_key": encounter.external_visit_key,
        "encounter_type": encounter.encounter_type,
        "started_at": encounter.started_at.isoformat() if encounter.started_at else None,
        "ended_at": encounter.ended_at.isoformat() if encounter.ended_at else None,
        "status": encounter.status,
        "created_at": encounter.created_at.isoformat(),
    }


async def get_patient_library_view(*, patient_id: str, current_uid: str, db: AsyncSession) -> dict:
    """患者库聚合视图:文档→版本→解析修订(含切块数)、快照代际、导入批次与时间线。

    时间线以已发布快照为主线(每次导入发布新快照),版本与修订挂在文档树下;
    只读,无任何状态变更。
    """
    from sqlalchemy import func

    from yuxi.repositories.patient_import_repository import PatientImportRepository
    from yuxi.storage.postgres.models_clinical import (
        PatientChunk,
        PatientDocument,
        PatientDocumentRevision,
        PatientDocumentVersion,
        PatientImportBatch,
        PatientSnapshot,
        PatientSnapshotMember,
    )

    repo = PatientRepository(db)
    patient = await repo.get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")

    documents = (
        await db.execute(
            select(PatientDocument)
            .where(PatientDocument.patient_id == patient.id)
            .order_by(PatientDocument.created_at.asc())
        )
    ).scalars().all()
    versions = (
        await db.execute(
            select(PatientDocumentVersion).where(PatientDocumentVersion.patient_id == patient.id)
        )
    ).scalars().all()
    versions_by_doc: dict[str, list] = {}
    for version in versions:
        versions_by_doc.setdefault(version.document_id, []).append(version)
    revision_ids = [version.id for version in versions]
    revisions = (
        await db.execute(
            select(PatientDocumentRevision).where(PatientDocumentRevision.document_version_id.in_(revision_ids))
            if revision_ids
            else select(PatientDocumentRevision).where(False)
        )
    ).scalars().all()
    revisions_by_version: dict[str, list] = {}
    for revision in revisions:
        revisions_by_version.setdefault(revision.document_version_id, []).append(revision)
    chunk_counts = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(PatientChunk.document_version_id, func.count(PatientChunk.chunk_id))
                .where(PatientChunk.document_version_id.in_(revision_ids))
                .group_by(PatientChunk.document_version_id)
                if revision_ids
                else select(PatientChunk.document_version_id, func.count()).where(False).group_by(
                    PatientChunk.document_version_id
                )
            )
        ).all()
    }

    document_tree = []
    for document in documents:
        doc_versions = sorted(versions_by_doc.get(document.id, []), key=lambda v: v.version)
        document_tree.append(
            {
                "id": document.id,
                "logical_key": document.logical_key,
                "document_type": document.document_type,
                "status": document.status,
                "visit_id": document.visit_id,
                "event_started_at": document.event_started_at.isoformat()
                if document.event_started_at
                else None,
                "versions": [
                    {
                        "id": version.id,
                        "version": version.version,
                        "status": version.status,
                        "content_hash": version.content_hash[:12],
                        "size": version.size,
                        "uploaded_at": version.uploaded_at.isoformat(),
                        "revisions": [
                            {
                                "id": revision.id,
                                "revision_version": revision.version,
                                "status": revision.status,
                                "approved_at": revision.approved_at.isoformat() if revision.approved_at else None,
                                "chunk_count": chunk_counts.get(version.id, 0) if revision.status == "approved" else 0,
                            }
                            for revision in sorted(
                                revisions_by_version.get(version.id, []), key=lambda r: r.version
                            )
                        ],
                    }
                    for version in doc_versions
                ],
            }
        )

    snapshots = (
        await db.execute(
            select(PatientSnapshot)
            .where(PatientSnapshot.patient_id == patient.id)
            .order_by(PatientSnapshot.sequence.asc())
        )
    ).scalars().all()
    snapshot_list = []
    for snapshot in snapshots:
        member_count = len(
            (
                await db.execute(
                    select(PatientSnapshotMember).where(PatientSnapshotMember.snapshot_id == snapshot.id)
                )
            ).scalars().all()
        )
        snapshot_list.append(
            {
                "id": snapshot.id,
                "sequence": snapshot.sequence,
                "status": snapshot.status,
                "member_count": member_count,
                "manifest_hash": snapshot.manifest_hash[:12],
                "published_at": snapshot.published_at.isoformat() if snapshot.published_at else None,
                "created_by_batch_id": snapshot.created_by_batch_id,
                "is_current": patient.current_snapshot_id == snapshot.id,
            }
        )

    batches = (
        await db.execute(
            select(PatientImportBatch)
            .where(PatientImportBatch.patient_id == patient.id)
            .order_by(PatientImportBatch.created_at.desc())
        )
    ).scalars().all()

    return {
        "patient": patient.to_dict(),
        "documents": document_tree,
        "snapshots": snapshot_list,
        "batches": [PatientImportRepository.serialize_batch(batch) for batch in batches],
        "timeline_note": "时间线按快照 sequence 演进;同一逻辑文档的新版本在发布后进入当前快照,旧版本保留在历史快照",
    }


async def get_revision_chunks_view(*, revision_id: str, current_uid: str, db: AsyncSession) -> dict:
    """查看一个解析修订的切块:内容、字符区间与页码。"""
    from yuxi.storage.postgres.models_clinical import PatientChunk, PatientDocumentRevision, PatientDocumentVersion

    revision = await db.scalar(select(PatientDocumentRevision).where(PatientDocumentRevision.id == revision_id))
    if revision is None:
        raise HTTPException(status_code=404, detail="解析修订不存在")
    version = await db.get(PatientDocumentVersion, revision.document_version_id)
    patient = await db.get(Patient, version.patient_id)
    if patient is None or not (
        patient.owner_uid == str(current_uid)
        or await PatientRepository(db).get_accessible(patient.id, str(current_uid))
    ):
        raise HTTPException(status_code=404, detail="解析修订不存在")
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
    return {
        "revision": {
            "id": revision.id,
            "document_version_id": version.id,
            "revision_version": revision.version,
            "status": revision.status,
        },
        "document_type": None,
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "chunk_index": chunk.chunk_index,
                "content": chunk.content,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "page_number": chunk.page_number,
            }
            for chunk in chunks
        ],
    }


async def delete_patient_view(*, patient_id: str, current_uid: str, db: AsyncSession) -> dict:
    """删除患者:软删患者行,同步清理病例数据(文档/版本/修订/切块/快照/批次)与向量。

    - 仅 Owner 可删除;协作授权者无权。
    - 会话绑定保留(不可变绑定的 RESTRICT 外键),绑定会话检索将得到患者已删除的明确失败。
    - 向量先按 chunk_id 清理,再删 PG 病例数据;患者行软删后不可再查询。
    """
    from yuxi.knowledge.patient_index import delete_orphan_vectors
    from yuxi.storage.postgres.models_clinical import (
        PatientChunk,
        PatientDocument,
        PatientDocumentRevision,
        PatientDocumentVersion,
        PatientImportBatch,
        PatientSnapshot,
        PatientSnapshotMember,
    )

    repo = PatientRepository(db)
    patient = await repo.get_accessible(patient_id, str(current_uid))
    if patient is None or patient.status == "deleted":
        raise HTTPException(status_code=404, detail="患者不存在")
    if patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可删除患者")

    chunk_ids = list(
        (await db.execute(select(PatientChunk.chunk_id).where(PatientChunk.patient_id == patient.id))).scalars()
    )
    if chunk_ids:
        await delete_orphan_vectors(chunk_ids)

    await db.execute(
        delete(PatientSnapshotMember).where(
            PatientSnapshotMember.snapshot_id.in_(
                select(PatientSnapshot.id).where(PatientSnapshot.patient_id == patient.id)
            )
        )
    )
    await db.execute(delete(PatientSnapshot).where(PatientSnapshot.patient_id == patient.id))
    await db.execute(delete(PatientChunk).where(PatientChunk.patient_id == patient.id))
    await db.execute(
        delete(PatientDocumentRevision).where(
            PatientDocumentRevision.document_version_id.in_(
                select(PatientDocumentVersion.id).where(PatientDocumentVersion.patient_id == patient.id)
            )
        )
    )
    await db.execute(delete(PatientDocumentVersion).where(PatientDocumentVersion.patient_id == patient.id))
    await db.execute(delete(PatientDocument).where(PatientDocument.patient_id == patient.id))
    await db.execute(delete(PatientImportBatch).where(PatientImportBatch.patient_id == patient.id))
    patient.status = "deleted"
    patient.current_snapshot_id = None
    await db.commit()
    return {
        "id": patient.id,
        "display_code": patient.display_code,
        "status": patient.status,
        "removed_chunks": len(chunk_ids),
    }
