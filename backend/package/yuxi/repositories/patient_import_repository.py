"""患者导入批次与文档版本的持久化 Repository,拥有批次状态机与版本唯一性事实。

状态机事实:仅允许合法转换;published/终态不可再变更;
身份冲突与未确认归属的批次不得进入 parsing 之后的可入库状态。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_clinical import (
    PATIENT_BATCH_ACTIVE_STATUSES,
    PatientDocument,
    PatientDocumentRevision,
    PatientDocumentVersion,
    PatientImportBatch,
)

# 批次合法状态转换;published 与 identity_conflict 是吸收态,离开它们需要独立纠正流程。
_ALLOWED_BATCH_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"identity_check", "cancelled", "failed"}),
    "identity_check": frozenset({"parsing", "review_required", "identity_conflict", "cancelled", "failed"}),
    "review_required": frozenset({"parsing", "indexing", "verifying", "cancelled", "failed"}),
    "parsing": frozenset({"review_required", "indexing", "failed", "cancelled"}),
    "indexing": frozenset({"verifying", "failed", "cancelled"}),
    "verifying": frozenset({"published", "failed", "cancelled"}),
    "published": frozenset(),
    "failed": frozenset({"parsing"}),
    "cancelled": frozenset(),
    "identity_conflict": frozenset(),
}


class PatientImportRepository:
    """读写导入批次、逻辑文档、不可变版本与解析修订。"""

    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def add_batch(self, batch: PatientImportBatch) -> PatientImportBatch:
        """新增批次并 flush。"""
        self.db.add(batch)
        await self.db.flush()
        return batch

    async def get_batch(self, batch_id: str, patient_id: str | None = None) -> PatientImportBatch | None:
        """读取批次;提供 patient_id 时强制归属一致。"""
        stmt = select(PatientImportBatch).where(PatientImportBatch.id == str(batch_id))
        if patient_id is not None:
            stmt = stmt.where(PatientImportBatch.patient_id == str(patient_id))
        return await self.db.scalar(stmt)

    async def lock_batch(self, batch_id: str) -> PatientImportBatch | None:
        """锁定批次行,串行化状态推进。"""
        return await self.db.scalar(
            select(PatientImportBatch).where(PatientImportBatch.id == str(batch_id)).with_for_update()
        )

    async def find_batch_by_idempotency_key(self, patient_id: str, idempotency_key: str) -> PatientImportBatch | None:
        """按患者与导入意图幂等键读取批次。"""
        return await self.db.scalar(
            select(PatientImportBatch).where(
                PatientImportBatch.patient_id == str(patient_id),
                PatientImportBatch.idempotency_key == str(idempotency_key),
            )
        )

    async def transition_batch(self, batch: PatientImportBatch, target_status: str) -> PatientImportBatch:
        """按合法转换表推进批次状态;非法转换抛错,不静默。"""
        allowed = _ALLOWED_BATCH_TRANSITIONS.get(batch.status, frozenset())
        if target_status not in allowed:
            raise ValueError(f"非法批次状态转换: {batch.status} -> {target_status}")
        batch.status = target_status
        await self.db.flush()
        return batch

    async def get_or_create_document(
        self,
        *,
        patient_id: str,
        document_type: str,
        logical_key: str,
        visit_id: str | None,
    ) -> tuple[PatientDocument, bool]:
        """按稳定 logical_key 读取或创建逻辑文档;返回 (文档, 是否新建)。"""
        existing = await self.db.scalar(
            select(PatientDocument).where(
                PatientDocument.patient_id == str(patient_id),
                PatientDocument.logical_key == str(logical_key),
            )
        )
        if existing is not None:
            return existing, False
        document = PatientDocument(
            id=_new_id(),
            patient_id=str(patient_id),
            visit_id=visit_id,
            document_type=document_type,
            logical_key=str(logical_key),
            status="active",
        )
        self.db.add(document)
        await self.db.flush()
        return document, True

    async def find_version_by_content_hash(self, patient_id: str, content_hash: str) -> PatientDocumentVersion | None:
        """识别同患者完全重复内容;重复导入幂等返回已有版本。"""
        return await self.db.scalar(
            select(PatientDocumentVersion).where(
                PatientDocumentVersion.patient_id == str(patient_id),
                PatientDocumentVersion.content_hash == str(content_hash),
                PatientDocumentVersion.status == "active",
            )
        )

    async def add_document_version(self, version: PatientDocumentVersion) -> PatientDocumentVersion:
        """新增不可变原件版本并 flush。"""
        self.db.add(version)
        await self.db.flush()
        return version

    async def next_document_version_number(self, document_id: str) -> int:
        """返回逻辑文档的下一个版本号。"""
        current = await self.db.scalar(
            select(func.max(PatientDocumentVersion.version)).where(
                PatientDocumentVersion.document_id == str(document_id)
            )
        )
        return int(current or 0) + 1

    async def add_revision(self, revision: PatientDocumentRevision) -> PatientDocumentRevision:
        """新增解析修订并 flush。"""
        self.db.add(revision)
        await self.db.flush()
        return revision

    async def batches_beyond_gate(self, patient_id: str) -> list[PatientImportBatch]:
        """列出已通过归属与身份门禁、等待入库推进的批次。"""
        result = await self.db.execute(
            select(PatientImportBatch).where(
                PatientImportBatch.patient_id == str(patient_id),
                PatientImportBatch.status.in_(PATIENT_BATCH_ACTIVE_STATUSES),
            )
        )
        return list(result.scalars().all())

    @staticmethod
    def gate_allows_processing(batch: PatientImportBatch) -> bool:
        """归属与身份门禁:matched 或已确认归属才允许解析入库。"""
        return batch.identity_status == "matched" or (
            batch.identity_status == "identity_unverified" and batch.assignment_status == "confirmed"
        )

    @staticmethod
    def serialize_batch(batch: PatientImportBatch) -> dict:
        """序列化批次公开状态;不含病历正文。"""
        return {
            "id": batch.id,
            "patient_id": batch.patient_id,
            "conversation_id": batch.conversation_id,
            "visit_id": batch.visit_id,
            "source_kind": batch.source_kind,
            "status": batch.status,
            "identity_status": batch.identity_status,
            "assignment_status": batch.assignment_status,
            "assignment_method": batch.assignment_method,
            "task_id": batch.task_id,
            "error_code": batch.error_code,
            "published_snapshot_id": batch.published_snapshot_id,
            "created_at": batch.created_at.isoformat(),
            "updated_at": batch.updated_at.isoformat(),
        }


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())
