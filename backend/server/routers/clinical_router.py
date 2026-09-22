"""ClinEvidence 患者/就诊路由:HTTP 保持薄,用例在 patient_service,可见性在 repository。"""

from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services import patient_record_ingest_service as patient_ingest
from yuxi.services import patient_snapshot_service as patient_snapshot
from yuxi.services import patient_service
from yuxi.storage.postgres.models_business import User

clinical = APIRouter(prefix="/clinical", tags=["clinical"])


class AssignmentConfirmRequest(BaseModel):
    """归属确认请求;确认方式 manual 或 manifest_review。"""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(max_length=32)


class PatientCreate(BaseModel):
    """创建患者;不接收任何真实身份字段。"""

    model_config = ConfigDict(extra="forbid")

    display_code: str | None = Field(None, max_length=64)
    identity_fingerprint: str | None = Field(None, max_length=128)


class PatientUpdate(BaseModel):
    """患者可维护字段白名单;身份字段与 Owner 不提供更新入口。"""

    model_config = ConfigDict(extra="forbid")

    display_code: str | None = Field(None, max_length=64)
    status: str | None = None


class EncounterCreate(BaseModel):
    """创建就诊;就诊归属必须有原文依据。"""

    model_config = ConfigDict(extra="forbid")

    encounter_type: str
    external_visit_key: str | None = Field(None, max_length=128)
    started_at: datetime | None = None
    ended_at: datetime | None = None


@clinical.post("/patients")
async def create_patient(
    payload: PatientCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """创建患者,创建者即 Owner。"""
    return await patient_service.create_patient_view(
        current_uid=str(current_user.uid),
        display_code=payload.display_code,
        identity_fingerprint=payload.identity_fingerprint,
        db=db,
    )


@clinical.get("/patients")
async def list_patients(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)):
    """列出当前用户可访问的患者。"""
    return await patient_service.list_patients_view(current_uid=str(current_user.uid), db=db)


@clinical.get("/patients/{patient_id}")
async def get_patient(
    patient_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """读取单个可访问患者;不可见与不存在同形 404。"""
    return await patient_service.get_patient_view(patient_id=patient_id, current_uid=str(current_user.uid), db=db)


@clinical.patch("/patients/{patient_id}")
async def update_patient(
    patient_id: str,
    payload: PatientUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """维护脱敏编号或归档状态;仅 Owner,身份字段不可触碰。"""
    return await patient_service.update_patient_view(
        patient_id=patient_id,
        current_uid=str(current_user.uid),
        display_code=payload.display_code,
        status=payload.status,
        db=db,
    )


@clinical.post("/patients/{patient_id}/encounters")
async def create_encounter(
    patient_id: str,
    payload: EncounterCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """为患者创建就诊记录。"""
    return await patient_service.create_encounter_view(
        patient_id=patient_id,
        current_uid=str(current_user.uid),
        encounter_type=payload.encounter_type,
        external_visit_key=payload.external_visit_key,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        db=db,
    )


@clinical.get("/patients/{patient_id}/encounters")
async def list_encounters(
    patient_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """列出患者就诊。"""
    return await patient_service.list_encounters_view(
        patient_id=patient_id, current_uid=str(current_user.uid), db=db
    )


# =============================================================================
# > === 病历导入分组:tmp 上传、批次确认与状态 ===
# =============================================================================

CLINICAL_TMP_PREFIX = "clinical-records-tmp"


class RecordConfirmRequest(BaseModel):
    """确认上传并创建导入批次;不接收 patient_id,归属由会话解析。"""

    model_config = ConfigDict(extra="forbid")

    tmp_file_ids: list[str] = Field(min_length=1, max_length=20)
    document_type: str = Field(max_length=64)
    visit_id: str | None = Field(None, max_length=64)
    event_started_at: datetime | None = None
    idempotency_key: str | None = Field(None, max_length=128)


@clinical.post("/threads/{thread_id}/records/tmp")
async def upload_record_tmp(
    thread_id: str,
    file: UploadFile,
    current_user: User = Depends(get_required_user),
):
    """上传病历原件到用户隔离的 tmp 路径,等待 confirm 建批次。"""
    return await patient_ingest.upload_record_tmp_view(
        thread_id=thread_id, file=file, current_uid=str(current_user.uid)
    )


@clinical.post("/threads/{thread_id}/records/confirm", status_code=201)
async def confirm_records(
    thread_id: str,
    payload: RecordConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """从 tmp 对象创建患者导入批次;身份与归属门禁在批次状态机中执行。"""
    return await patient_ingest.confirm_record_upload_view(
        thread_id=thread_id,
        current_uid=str(current_user.uid),
        tmp_file_ids=payload.tmp_file_ids,
        document_type=payload.document_type,
        visit_id=payload.visit_id,
        event_started_at=payload.event_started_at,
        idempotency_key=payload.idempotency_key,
        db=db,
    )


@clinical.get("/import-batches/{batch_id}")
async def get_import_batch(
    batch_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """读取导入批次状态。"""
    return await patient_ingest.get_batch_view(batch_id=batch_id, current_uid=str(current_user.uid), db=db)


@clinical.post("/import-batches/{batch_id}/confirm-identity")
async def confirm_import_identity(
    batch_id: str,
    payload: AssignmentConfirmRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_required_user),
):
    """归属确认门禁:仅患者 Owner;确认不改变身份检测结果。"""
    return await patient_ingest.confirm_batch_assignment_view(
        batch_id=batch_id, current_uid=str(current_user.uid), method=payload.method, db=db
    )


@clinical.post("/import-batches/{batch_id}/cancel")
async def cancel_import_batch(
    batch_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """取消处理中的批次。"""
    return await patient_ingest.cancel_batch_view(batch_id=batch_id, current_uid=str(current_user.uid), db=db)


# =============================================================================
# > === 审核、索引与发布分组:Owner-only 的发布链路触发点 ===
# =============================================================================


@clinical.post("/revisions/{revision_id}/approve")
async def approve_revision(
    revision_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """人工内容审核:review_required → approved。"""
    return await patient_snapshot.approve_revision(
        revision_id=revision_id, current_uid=str(current_user.uid), db=db
    )


@clinical.post("/revisions/{revision_id}/chunks")
async def build_revision_chunks(
    revision_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """为已审核修订构建患者文块。"""
    return await patient_snapshot.build_and_store_chunks(
        revision_id=revision_id, current_uid=str(current_user.uid), db=db
    )


@clinical.post("/revisions/{revision_id}/index")
async def index_revision(
    revision_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """将文块编码写入患者向量投影;未配置向量模型时显式失败。"""
    return await patient_snapshot.index_revision_chunks(
        revision_id=revision_id, current_uid=str(current_user.uid), db=db
    )


@clinical.post("/import-batches/{batch_id}/finalize")
async def finalize_import_batch(
    batch_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """Owner 一键收口:审核 → 建块 → 写向量 → 发布;每步幂等,可重试。"""
    return await patient_snapshot.finalize_batch(
        batch_id=batch_id, current_uid=str(current_user.uid), db=db
    )


@clinical.delete("/patients/{patient_id}")
async def delete_patient(
    patient_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """删除患者:软删患者行并清理病例数据与向量;仅 Owner。"""
    return await patient_service.delete_patient_view(
        patient_id=patient_id, current_uid=str(current_user.uid), db=db
    )


@clinical.get("/patients/{patient_id}/library")
async def get_patient_library(
    patient_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """患者库聚合视图:文档→版本→修订(切块数)、快照代际、导入批次。"""
    return await patient_service.get_patient_library_view(
        patient_id=patient_id, current_uid=str(current_user.uid), db=db
    )


@clinical.get("/revisions/{revision_id}/chunks")
async def get_revision_chunks(
    revision_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """查看一个解析修订的切块明细。"""
    return await patient_service.get_revision_chunks_view(
        revision_id=revision_id, current_uid=str(current_user.uid), db=db
    )


@clinical.post("/import-batches/{batch_id}/publish")
async def publish_import_batch(
    batch_id: str, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_required_user)
):
    """原子发布患者快照;向量核对失败或成员缺失时拒绝并保持 PG 事实不变。"""
    return await patient_snapshot.publish_snapshot(
        batch_id=batch_id, current_uid=str(current_user.uid), db=db
    )
