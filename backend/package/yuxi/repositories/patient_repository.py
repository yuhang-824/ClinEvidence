"""患者域持久化 Repository:可见性、患者、就诊。所有患者读取必须经过同一可见性条件。"""

import uuid

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_clinical import Encounter, Patient, PatientAccessGrant
from yuxi.utils.datetime_utils import utc_now_naive


def new_clinical_id() -> str:
    """生成患者域资源 UUID。"""
    return str(uuid.uuid4())


class PatientRepository:
    """读写患者、授权与就诊事实;可见性由 owner 或有效 grant 构成。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    @staticmethod
    def accessibility_condition(uid: str):
        """返回 Owner 或有效授权的可见性 SQL 条件,供所有患者查询复用。"""
        valid_grant = exists(
            select(PatientAccessGrant.id).where(
                PatientAccessGrant.patient_id == Patient.id,
                PatientAccessGrant.grantee_uid == str(uid),
                or_(PatientAccessGrant.expires_at.is_(None), PatientAccessGrant.expires_at > utc_now_naive()),
            )
        )
        return or_(Patient.owner_uid == str(uid), valid_grant)

    async def add(self, patient: Patient) -> Patient:
        """新增患者并 flush,供外层事务继续绑定。"""
        self.db.add(patient)
        await self.db.flush()
        return patient

    async def get_accessible(self, patient_id: str, uid: str) -> Patient | None:
        """按可见性条件读取患者;不可见与不存在同形返回空。"""
        return await self.db.scalar(
            select(Patient).where(Patient.id == str(patient_id), self.accessibility_condition(uid))
        )

    async def get_accessible_active(self, patient_id: str, uid: str) -> Patient | None:
        """读取可访问且未删除的患者。"""
        patient = await self.get_accessible(patient_id, uid)
        if patient is None or patient.status == "deleted":
            return None
        return patient

    async def lock_accessible(self, patient_id: str, uid: str) -> Patient | None:
        """锁定可访问患者行,用于绑定与导入等需要串行化的写路径。"""
        return await self.db.scalar(
            select(Patient)
            .where(Patient.id == str(patient_id), self.accessibility_condition(uid))
            .with_for_update()
        )

    async def list_accessible(self, uid: str, *, include_deleted: bool = False) -> list[Patient]:
        """列出用户可访问的患者,按名称排序。"""
        stmt = select(Patient).where(self.accessibility_condition(uid))
        if not include_deleted:
            stmt = stmt.where(Patient.status != "deleted")
        result = await self.db.execute(stmt.order_by(Patient.display_code.asc(), Patient.id.asc()))
        return list(result.scalars().all())

    async def display_code_exists(self, display_code: str) -> bool:
        """检查脱敏编号是否已占用。"""
        existing = await self.db.scalar(select(Patient.id).where(Patient.display_code == str(display_code)))
        return existing is not None

    async def add_grant(self, grant: PatientAccessGrant) -> PatientAccessGrant:
        """新增协作授权并 flush。"""
        self.db.add(grant)
        await self.db.flush()
        return grant

    async def add_encounter(self, encounter: Encounter) -> Encounter:
        """新增就诊并 flush。"""
        self.db.add(encounter)
        await self.db.flush()
        return encounter

    async def get_encounter(self, encounter_id: str, patient_id: str) -> Encounter | None:
        """读取患者名下的就诊;跨患者读取一律为空。"""
        return await self.db.scalar(
            select(Encounter).where(Encounter.id == str(encounter_id), Encounter.patient_id == str(patient_id))
        )

    async def list_encounters(self, patient_id: str) -> list[Encounter]:
        """列出患者全部就诊,按开始时间升序、未知时间在后。"""
        result = await self.db.execute(
            select(Encounter)
            .where(Encounter.patient_id == str(patient_id))
            .order_by(Encounter.started_at.asc().nullslast(), Encounter.created_at.asc())
        )
        return list(result.scalars().all())
