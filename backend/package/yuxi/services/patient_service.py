"""患者域用例服务:创建、查询、脱敏编号与归档、就诊管理。

权限边界:可见性最终在 repository 查询执行;服务层只编排用例与校验。
patients 行的身份字段(owner_uid、identity_fingerprint)与绑定关系不提供任何更新入口。
"""

import secrets

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.patient_repository import PatientRepository, new_clinical_id
from yuxi.storage.postgres.models_clinical import Encounter, Patient

_DISPLAY_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_MAX_DISPLAY_CODE_ATTEMPTS = 8


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
    identity_fingerprint: str | None = None,
    db: AsyncSession,
) -> dict:
    """创建患者;创建者即 Owner。"""
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
    status: str | None = None,
    db: AsyncSession,
) -> dict:
    """更新脱敏编号或归档状态;仅 Owner 可操作,身份字段不可触碰。"""
    repo = PatientRepository(db)
    patient = await repo.get_accessible_active(patient_id, str(current_uid))
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if patient.owner_uid != str(current_uid):
        raise HTTPException(status_code=403, detail="仅患者 Owner 可维护患者资料")
    if status is not None and status not in {"active", "archived"}:
        raise HTTPException(status_code=400, detail="status 仅支持 active/archived;删除走独立清理流程")
    if display_code is not None:
        normalized_code = str(display_code).strip()
        if not normalized_code:
            raise HTTPException(status_code=400, detail="脱敏编号不能为空")
        if normalized_code != patient.display_code and await repo.display_code_exists(normalized_code):
            raise HTTPException(status_code=409, detail="脱敏编号已存在")
        patient.display_code = normalized_code
    if status is not None:
        patient.status = status
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
