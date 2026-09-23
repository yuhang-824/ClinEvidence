"""患者问答 RAGAS 数据集和运行记录的持久化边界。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_clinical import ClinicalRagasDataset, ClinicalRagasRun, Patient


class ClinicalRagasRepository:
    """在查询层执行用户隔离并保存评估事实。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_dataset(self, dataset_id: str, uid: str) -> ClinicalRagasDataset | None:
        """只返回当前用户拥有的数据集。"""
        return await self.db.scalar(
            select(ClinicalRagasDataset).where(
                ClinicalRagasDataset.id == dataset_id, ClinicalRagasDataset.owner_uid == uid
            )
        )

    async def list_datasets(self, uid: str) -> list[ClinicalRagasDataset]:
        """按创建时间列出当前用户的数据集。"""
        rows = await self.db.scalars(
            select(ClinicalRagasDataset)
            .where(ClinicalRagasDataset.owner_uid == uid)
            .order_by(ClinicalRagasDataset.created_at.desc())
        )
        return list(rows.all())

    async def get_run(self, run_id: str, uid: str) -> ClinicalRagasRun | None:
        """只返回当前用户拥有的评估运行。"""
        return await self.db.scalar(
            select(ClinicalRagasRun).where(ClinicalRagasRun.id == run_id, ClinicalRagasRun.owner_uid == uid)
        )

    async def list_runs(self, uid: str) -> list[ClinicalRagasRun]:
        """按创建时间列出当前用户的评估运行。"""
        rows = await self.db.scalars(
            select(ClinicalRagasRun)
            .where(ClinicalRagasRun.owner_uid == uid)
            .order_by(ClinicalRagasRun.created_at.desc())
            .limit(50)
        )
        return list(rows.all())

    async def get_patient_by_code(self, display_code: str, uid: str) -> Patient | None:
        """只允许 Owner 在评估中复制患者证据到持久结果。"""
        return await self.db.scalar(
            select(Patient).where(
                Patient.display_code == display_code,
                Patient.status != "deleted",
                Patient.owner_uid == uid,
            )
        )
