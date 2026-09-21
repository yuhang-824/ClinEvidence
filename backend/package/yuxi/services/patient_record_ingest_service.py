"""患者病历导入用例:批次创建、身份检测、归属确认门禁与入库推进。

事实边界:
- 上传作用域由服务端从会话解析;线程上传不接收 patient_id。
- 身份检测只比对已有稳定标识;脱敏资料保留 identity_unverified,归属靠独立确认。
- CLI 种子与线程上传在同一服务汇合,禁止旁路发布。
"""

import hashlib
import os
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.patient_import_repository import PatientImportRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.storage.minio.client import get_minio_client
from yuxi.storage.postgres.models_clinical import (
    PATIENT_BATCH_SOURCE_KINDS,
    PatientDocumentRevision,
    PatientDocumentVersion,
    PatientImportBatch,
)
from yuxi.utils.datetime_utils import utc_now_naive
from yuxi.utils.logging_config import logger

_IDENTITY_SIGNAL_MIN_LENGTH = 8
_IDENTITY_SALT_ENV = "YUXI_IDENTITY_HASH_SALT"


def hash_identity_value(value: str, salt: str) -> str:
    """标准化患者标识的加盐哈希;指纹写入方与比对方必须共用本函数。"""
    normalized = "".join(ch for ch in value.strip() if ch.isalnum()).lower()
    return hashlib.sha256((salt + ":" + normalized).encode("utf-8")).hexdigest()


def detect_identity_signals(text: str) -> list[str]:
    """从解析文本提取候选身份标识(长数字串);值只在内存比对,不落库、不进日志。"""
    tokens = "".join(ch if ch.isdigit() else " " for ch in text or "").split()
    return [token for token in tokens if len(token) >= _IDENTITY_SIGNAL_MIN_LENGTH][:5]


def evaluate_identity_status(patient_fingerprint: str | None, signals: list[str]) -> str:
    """按信号与已登记指纹判定身份状态。

    - 指纹未登记(脱敏资料常态):identity_unverified,等待归属确认;
    - 指纹已登记且任一信号哈希匹配:matched;
    - 指纹已登记但全部信号不匹配:identity_conflict,批次隔离。
    """
    if patient_fingerprint is None:
        return "identity_unverified"
    salt = os.environ.get(_IDENTITY_SALT_ENV, "")
    if not salt:
        logger.warning("identity fingerprint registered but salt is not configured; treating as unverified")
        return "identity_unverified"
    for token in signals:
        if hash_identity_value(token, salt) == patient_fingerprint:
            return "matched"
    return "identity_conflict"


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patient_object_key(patient_id: str, version_id: str, filename: str) -> str:
    """原件对象键只含 UUID 路径,不含姓名、住院号或诊断。"""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return f"clinical/{patient_id[:2]}/{patient_id}/{version_id}{suffix}"


async def create_thread_upload_batch(
    *,
    thread_id: str,
    current_uid: str,
    files: list[dict],
    document_type: str,
    visit_id: str | None = None,
    logical_key_prefix: str | None = None,
    idempotency_key: str | None = None,
    db: AsyncSession,
) -> dict:
    """从会话上传创建患者导入批次;患者归属来自会话绑定,不来自请求。"""
    from yuxi.repositories.conversation_repository import ConversationRepository
    from yuxi.services.conversation_service import require_user_conversation

    conversation = await require_user_conversation(ConversationRepository(db), thread_id, str(current_uid))
    if not conversation.patient_id:
        raise HTTPException(status_code=400, detail="会话未绑定患者,不能导入病历")
    return await _create_batch(
        patient_id=conversation.patient_id,
        conversation_id=conversation.id,
        source_kind="thread_upload",
        current_uid=str(current_uid),
        files=files,
        document_type=document_type,
        visit_id=visit_id,
        logical_key_prefix=logical_key_prefix,
        idempotency_key=idempotency_key,
        db=db,
    )


async def create_seed_batch(
    *,
    patient_id: str,
    current_uid: str,
    files: list[dict],
    document_type: str,
    dataset_key: str,
    manifest_hash: str,
    visit_id: str | None = None,
    logical_key_prefix: str | None = None,
    idempotency_key: str | None = None,
    db: AsyncSession,
) -> dict:
    """种子导入创建批次;调用方必须具备服务端授予的种子导入权限(由 CLI 适配层校验)。"""
    return await _create_batch(
        patient_id=patient_id,
        conversation_id=None,
        source_kind="seed",
        current_uid=str(current_uid),
        files=files,
        document_type=document_type,
        visit_id=visit_id,
        logical_key_prefix=logical_key_prefix,
        idempotency_key=idempotency_key,
        dataset_key=dataset_key,
        manifest_hash=manifest_hash,
        db=db,
    )


async def _create_batch(
    *,
    patient_id: str,
    conversation_id: int | None,
    source_kind: str,
    current_uid: str,
    files: list[dict],
    document_type: str,
    visit_id: str | None,
    logical_key_prefix: str | None,
    idempotency_key: str | None,
    db: AsyncSession,
    dataset_key: str | None = None,
    manifest_hash: str | None = None,
) -> dict:
    if source_kind not in PATIENT_BATCH_SOURCE_KINDS:
        raise HTTPException(status_code=400, detail="未知导入来源")
    patient_repo = PatientRepository(db)
    patient = await patient_repo.lock_accessible(patient_id, current_uid)
    if patient is None or patient.status != "active":
        raise HTTPException(status_code=404, detail="患者不存在")
    if visit_id is not None:
        encounter = await patient_repo.get_encounter(visit_id, patient.id)
        if encounter is None:
            raise HTTPException(status_code=404, detail="就诊不存在或不属于该患者")

    import_repo = PatientImportRepository(db)
    if idempotency_key:
        existing = await import_repo.find_batch_by_idempotency_key(patient.id, idempotency_key)
        if existing is not None:
            return PatientImportRepository.serialize_batch(existing)

    batch = await import_repo.add_batch(
        PatientImportBatch(
            id=str(uuid.uuid4()),
            patient_id=patient.id,
            conversation_id=conversation_id,
            visit_id=visit_id,
            source_kind=source_kind,
            dataset_key=dataset_key,
            manifest_hash=manifest_hash,
            idempotency_key=idempotency_key,
            status="uploaded",
            identity_status="pending",
            assignment_status="pending",
            requested_by=current_uid,
        )
    )
    version_ids = []
    for item in files:
        version_ids.append(
            await _store_original(
                import_repo,
                patient_id=patient.id,
                batch=batch,
                filename=item["filename"],
                data=item["data"],
                document_type=document_type,
                logical_key_prefix=logical_key_prefix,
            )
        )
    await import_repo.transition_batch(batch, "identity_check")
    task = await _enqueue_ingest_task(batch, db)
    batch.task_id = task.id
    await db.commit()
    # 投递严格晚于 owning transaction 提交;worker 扑空不再可能。
    from yuxi.services.task_service import tasker

    await tasker.publish(task)
    serialized = PatientImportRepository.serialize_batch(batch)
    serialized["document_version_ids"] = version_ids
    return serialized


async def _store_original(
    import_repo: PatientImportRepository,
    *,
    patient_id: str,
    batch: PatientImportBatch,
    filename: str,
    data: bytes,
    document_type: str,
    logical_key_prefix: str | None,
) -> str:
    """保存不可变原件并建立逻辑文档与版本;同患者同内容幂等返回已有版本。"""
    content_hash = _content_hash(data)
    duplicate = await import_repo.find_version_by_content_hash(patient_id, content_hash)
    if duplicate is not None:
        logger.info(f"patient import duplicate content ignored: batch={batch.id} version={duplicate.id}")
        return duplicate.id

    version_id = str(uuid.uuid4())
    object_key = _patient_object_key(patient_id, version_id, filename)
    minio_client = get_minio_client()
    bucket = minio_client.KB_BUCKETS["documents"]
    await minio_client.aupload_file(bucket, object_key, data)

    logical_key = f"{logical_key_prefix or 'doc'}:{content_hash[:16]}"
    document, _created = await import_repo.get_or_create_document(
        patient_id=patient_id,
        document_type=document_type,
        logical_key=logical_key,
        visit_id=batch.visit_id,
    )
    version_number = await import_repo.next_document_version_number(document.id)
    version = await import_repo.add_document_version(
        PatientDocumentVersion(
            id=version_id,
            document_id=document.id,
            patient_id=patient_id,
            version=version_number,
            import_batch_id=batch.id,
            content_hash=content_hash,
            original_object_key=object_key,
            mime_type="application/pdf" if filename.lower().endswith(".pdf") else None,
            size=len(data),
            uploaded_at=utc_now_naive(),
            status="active",
        )
    )
    return version.id


async def confirm_batch_assignment(
    *,
    batch_id: str,
    current_uid: str,
    method: str,
    db: AsyncSession,
) -> dict:
    """归属确认门禁:确认者必须是患者 Owner;确认不改变身份检测结果,不伪装 matched。"""
    if method not in {"manual", "manifest_review"}:
        raise HTTPException(status_code=400, detail="未知归属确认方式")
    import_repo = PatientImportRepository(db)
    batch = await import_repo.lock_batch(str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    patient_repo = PatientRepository(db)
    patient = await patient_repo.get_accessible(batch.patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    if patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可确认归属")
    if batch.identity_status == "identity_conflict":
        raise HTTPException(status_code=409, detail="身份冲突批次不可通过归属确认放行")
    if batch.assignment_status == "confirmed":
        raise HTTPException(status_code=409, detail="批次归属已确认,不可重复确认或改派")
    if batch.status not in {"identity_check", "review_required"}:
        raise HTTPException(status_code=409, detail=f"批次状态 {batch.status} 不接受归属确认")
    batch.assignment_status = "confirmed"
    batch.assignment_method = method
    batch.assigned_by = str(current_uid)
    batch.assigned_at = utc_now_naive()
    # 门禁通过后重新入队解析任务;停留在 identity_check 的批次由确认动作恢复推进。
    task = None
    if batch.status == "identity_check" and batch.identity_status != "matched":
        task = await _enqueue_ingest_task(batch, db)
        batch.task_id = task.id
    await db.commit()
    if task is not None:
        from yuxi.services.task_service import tasker

        await tasker.publish(task)
    return PatientImportRepository.serialize_batch(batch)


async def cancel_batch(*, batch_id: str, current_uid: str, db: AsyncSession) -> dict:
    """取消处理中的批次;已发布批次不可取消。"""
    import_repo = PatientImportRepository(db)
    batch = await import_repo.lock_batch(str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    patient = await PatientRepository(db).get_accessible(batch.patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    try:
        await import_repo.transition_batch(batch, "cancelled")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    return PatientImportRepository.serialize_batch(batch)


async def _enqueue_ingest_task(batch: PatientImportBatch, db: AsyncSession):
    """在 owning transaction 内创建任务;调用方必须在 commit 后调用 tasker.publish。"""
    from yuxi.services.task_service import tasker

    return await tasker.create_in_session(
        db,
        name=f"patient-record-ingest:{batch.id}",
        task_type="patient_record_ingest",
        payload={"batch_id": batch.id, "operator_id": batch.requested_by},
    )


async def run_patient_record_ingest(context) -> dict:
    """Durable Task handler:身份检测 → 门禁检查 → 解析落修订。

    未通过归属/身份门禁的批次保持 identity_check 可观察状态,不标记失败;
    解析完成后批次进入 review_required,等待人工内容审核(阶段 3 接索引与发布)。
    """
    from yuxi.storage.postgres.manager import pg_manager

    batch_id = context.payload["batch_id"]
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        import_repo = PatientImportRepository(db)
        batch = await import_repo.lock_batch(batch_id)
        if batch is None:
            raise RuntimeError(f"import batch missing: {batch_id}")
        patient = await PatientRepository(db).get_accessible_active(batch.patient_id, batch.requested_by)
        if patient is None:
            raise RuntimeError(f"patient missing for batch: {batch_id}")

        await context.set_progress(10.0, "身份信号检测")
        versions = await _active_versions_for_batch(import_repo, batch.id)
        # 重试幂等:已落修订的版本跳过解析,不重复创建。
        pending = [
            version for version in versions if not await _has_revision(import_repo, version.id)
        ]
        signals: list[str] = []
        parsed = await _parse_batch_originals(context, pending, signals)
        batch.identity_status = evaluate_identity_status(patient.identity_fingerprint, signals)
        if batch.identity_status == "identity_conflict":
            await import_repo.transition_batch(batch, "identity_conflict")
            await db.commit()
            return {"batch_id": batch.id, "status": batch.status, "identity_status": batch.identity_status}

        if not PatientImportRepository.gate_allows_processing(batch):
            await db.commit()
            return {"batch_id": batch.id, "status": batch.status, "identity_status": batch.identity_status}

        await import_repo.transition_batch(batch, "parsing")
        await db.commit()

        for version, (raw_text, markdown, report) in zip(pending, parsed, strict=False):
            revision = PatientDocumentRevision(
                id=str(uuid.uuid4()),
                document_version_id=version.id,
                version=1,
                raw_content=raw_text,
                content=markdown,
                structure_report=report,
                parser_id="docling",
                status="review_required",
            )
            await import_repo.add_revision(revision)
            await context.raise_if_cancelled()
            await context.set_progress(80.0, f"解析完成 {version.id}")

        await import_repo.transition_batch(batch, "review_required")
        batch.error_code = None
        await db.commit()
        return {"batch_id": batch.id, "status": batch.status, "identity_status": batch.identity_status}


async def _has_revision(import_repo: PatientImportRepository, version_id: str) -> bool:
    """检查版本是否已落解析修订;worker 重试幂等的依据。"""
    from sqlalchemy import select

    from yuxi.storage.postgres.models_clinical import PatientDocumentRevision

    found = await import_repo.db.scalar(
        select(PatientDocumentRevision.id).where(PatientDocumentRevision.document_version_id == str(version_id))
    )
    return found is not None


async def _active_versions_for_batch(import_repo: PatientImportRepository, batch_id: str):
    """列出批次名下的 active 原件版本。"""
    from yuxi.storage.postgres.models_clinical import PatientDocumentVersion

    result = await import_repo.db.execute(
        select(PatientDocumentVersion).where(PatientDocumentVersion.import_batch_id == str(batch_id))
    )
    return list(result.scalars().all())


async def _parse_batch_originals(context, versions, signals: list[str]) -> list[tuple]:
    """下载原件并执行解析;同时收集身份信号。解析失败走 failure hook 显式收敛。"""
    from yuxi.services.ocr_service import parse_knowledge_document
    from yuxi.storage.minio.client import get_minio_client

    minio_client = get_minio_client()
    bucket = minio_client.KB_BUCKETS["documents"]
    parsed = []
    for index, version in enumerate(versions, 1):
        await context.raise_if_cancelled()
        data = await minio_client.adownload_file(bucket, version.original_object_key)
        signals.extend(detect_identity_signals(data.decode("utf-8", errors="ignore")[:20000]))
        source = f"minio://{bucket}/{version.original_object_key}"
        raw, content, report = await parse_knowledge_document(
            source,
            {},
            kb_id="clinical",
            file_id=version.id,
        )
        parsed.append((raw, content, report if isinstance(report, dict) else {}))
        await context.set_progress(10.0 + 60.0 * index / max(len(versions), 1), f"解析 {version.id}")
    return parsed


async def fail_patient_record_ingest(context, error: str) -> None:
    """任务失败收敛:批次落 failed 并保留错误码,不更新任何已发布事实。"""
    from yuxi.storage.postgres.manager import pg_manager

    batch_id = (context.payload or {}).get("batch_id")
    if not batch_id:
        return
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        import_repo = PatientImportRepository(db)
        batch = await import_repo.lock_batch(batch_id)
        if batch is None or batch.status == "published":
            return
        try:
            await import_repo.transition_batch(batch, "failed")
        except ValueError:
            logger.warning(f"batch {batch_id} terminal status keeps {batch.status} after task failure")
            return
        batch.error_code = "ingest_failed"
        await db.commit()


CLINICAL_TMP_PREFIX = "clinical-records-tmp"
_TMP_UPLOAD_MAX_BYTES = 200 * 1024 * 1024


async def upload_record_tmp_view(*, thread_id: str, file, current_uid: str) -> dict:
    """上传病历原件到用户隔离 tmp 路径;不创建任何持久业务事实。"""
    from yuxi.services.attachment_service import read_upload_with_limit

    if not file or not getattr(file, "filename", None):
        raise HTTPException(status_code=400, detail="无法识别的文件名")
    safe_name = os.path.basename(file.filename).replace("\\", "_") or "record.pdf"
    data = await read_upload_with_limit(
        file,
        max_size_bytes=_TMP_UPLOAD_MAX_BYTES,
        too_large_message="病历文件超过大小限制",
    )
    tmp_file_id = uuid.uuid4().hex
    object_key = f"{CLINICAL_TMP_PREFIX}/{current_uid}/{tmp_file_id}/original/{safe_name}"
    minio_client = get_minio_client()
    bucket = minio_client.KB_BUCKETS["documents"]
    await minio_client.aupload_file(bucket, object_key, data)
    return {"tmp_file_id": tmp_file_id, "filename": safe_name, "size": len(data)}


async def confirm_record_upload_view(
    *,
    thread_id: str,
    current_uid: str,
    tmp_file_ids: list[str],
    document_type: str,
    visit_id: str | None,
    idempotency_key: str | None,
    db: AsyncSession,
) -> dict:
    """从 tmp 对象读取原件并创建批次;确认后删除 tmp 对象。"""
    if not document_type or not document_type.strip():
        raise HTTPException(status_code=400, detail="document_type 不能为空")
    minio_client = get_minio_client()
    bucket = minio_client.KB_BUCKETS["documents"]
    files: list[dict] = []
    for tmp_id in tmp_file_ids:
        if not re.fullmatch(r"[0-9a-f]{32}", str(tmp_id)):
            raise HTTPException(status_code=400, detail="tmp_file_id 非法")
        object_prefix = f"{CLINICAL_TMP_PREFIX}/{current_uid}/{tmp_id}/original/"
        objects = await minio_client.alist_object_metadata(bucket, object_prefix)
        if not objects:
            raise HTTPException(status_code=404, detail=f"tmp 对象不存在: {tmp_id}")
        object_key = objects[0].get("object_name") or objects[0].get("name")
        if not object_key:
            raise HTTPException(status_code=404, detail=f"tmp 对象不存在: {tmp_id}")
        data = await minio_client.adownload_file(bucket, object_key)
        files.append({"filename": object_key.rsplit("/", 1)[-1], "data": data})

    batch = await create_thread_upload_batch(
        thread_id=thread_id,
        current_uid=current_uid,
        files=files,
        document_type=document_type.strip(),
        visit_id=visit_id,
        idempotency_key=idempotency_key,
        db=db,
    )
    for tmp_id in tmp_file_ids:
        await minio_client.adelete_objects_by_prefix(
            bucket, f"{CLINICAL_TMP_PREFIX}/{current_uid}/{tmp_id}/original/"
        )
    return batch


async def get_batch_view(*, batch_id: str, current_uid: str, db: AsyncSession) -> dict:
    """读取批次状态;不可见患者名下的批次与不存在同形 404。"""
    import_repo = PatientImportRepository(db)
    batch = await import_repo.get_batch(str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    patient = await PatientRepository(db).get_accessible(batch.patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    return PatientImportRepository.serialize_batch(batch)


async def confirm_batch_assignment_view(*, batch_id: str, current_uid: str, method: str, db: AsyncSession) -> dict:
    """路由薄包装:归属确认门禁。"""
    return await confirm_batch_assignment(batch_id=batch_id, current_uid=current_uid, method=method, db=db)


async def cancel_batch_view(*, batch_id: str, current_uid: str, db: AsyncSession) -> dict:
    """路由薄包装:取消批次。"""
    return await cancel_batch(batch_id=batch_id, current_uid=current_uid, db=db)


SEED_OPERATORS_ENV = "YUXI_SEED_OPERATOR_UIDS"


async def require_seed_operator(db: AsyncSession, uid: str) -> None:
    """校验种子导入权限:仅环境显式列出的操作员 UID 可执行,不随角色隐式扩大。"""
    allowed = {item.strip() for item in os.environ.get(SEED_OPERATORS_ENV, "").split(",") if item.strip()}
    if str(uid) not in allowed:
        raise HTTPException(status_code=403, detail="当前操作员不具备种子导入权限")
    from yuxi.storage.postgres.models_business import User

    user = await db.scalar(select(User).where(User.uid == str(uid)))
    if user is None:
        raise HTTPException(status_code=403, detail="种子操作员不存在")


async def seed_patient_files(
    *,
    operator_uid: str,
    patient_key: str,
    files: list[dict],
    dataset_key: str,
    manifest_hash: str,
    db: AsyncSession,
) -> dict:
    """按 manifest 装机一名患者:确保患者存在(Owner=操作者),再经同一批次管线导入全部文件。

    归属依据是已审核清单(manifest_review);批次创建后等待 worker 解析与发布,
    本函数不直接写快照或向量。
    """
    await require_seed_operator(db, operator_uid)
    patient_repo = PatientRepository(db)
    patient = patient_by_display_code = None
    for candidate in await patient_repo.list_accessible(str(operator_uid)):
        if candidate.display_code == patient_key:
            patient_by_display_code = candidate
            break
    patient = patient_by_display_code
    if patient is None:
        from yuxi.services.patient_service import create_patient_view

        await create_patient_view(
            current_uid=operator_uid,
            display_code=patient_key,
            db=db,
        )
        for candidate in await patient_repo.list_accessible(str(operator_uid)):
            if candidate.display_code == patient_key:
                patient = candidate
                break
    if patient is None:
        raise HTTPException(status_code=500, detail="患者装机失败:创建后不可见")
    return await create_seed_batch(
        patient_id=patient.id,
        current_uid=str(operator_uid),
        files=files,
        document_type="medical_record",
        dataset_key=dataset_key,
        manifest_hash=manifest_hash,
        logical_key_prefix=dataset_key,
        idempotency_key=f"{dataset_key}:{patient_key}:{manifest_hash[:16]}",
        db=db,
    )
