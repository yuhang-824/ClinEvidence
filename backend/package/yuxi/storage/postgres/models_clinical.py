"""ClinEvidence 患者域数据模型:患者、授权、就诊、导入批次、文档版本、解析修订、文块与快照。

复用业务 Base,与 models_business 同一 metadata 交给 storage-migrator 独占建表。
不可变事实(文档版本、快照成员)不提供更新路径;current_snapshot_id 由快照发布事务维护,
不设跨表外键以避免循环依赖,一致性由 patient_snapshot_repository 发布事务拥有。
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)

from yuxi.storage.postgres.models_business import JSON_VALUE, Base
from yuxi.utils.datetime_utils import utc_now_naive

PATIENT_STATUS_SQL = "status IN ('active', 'archived', 'deleted')"
PATIENT_CATEGORY_PREFIXES = ("内膜癌", "宫颈癌", "卵巢癌")
PATIENT_CATEGORY_SQL = "category IS NULL OR (category = TRIM(category) AND length(category) BETWEEN 1 AND 32)"

PATIENT_BATCH_SOURCE_KINDS = ("thread_upload", "seed")
PATIENT_BATCH_ACTIVE_STATUSES = (
    "uploaded",
    "identity_check",
    "parsing",
    "review_required",
    "indexing",
    "verifying",
)
PATIENT_BATCH_TERMINAL_STATUSES = ("published", "failed", "cancelled", "identity_conflict")

PATIENT_BATCH_STATUS_SQL = (
    "status IN ('uploaded', 'identity_check', 'parsing', 'review_required', 'indexing', "
    "'verifying', 'published', 'failed', 'cancelled', 'identity_conflict')"
)
PATIENT_IDENTITY_STATUS_SQL = "identity_status IN ('pending', 'matched', 'identity_unverified', 'identity_conflict')"
PATIENT_ASSIGNMENT_STATUS_SQL = "assignment_status IN ('pending', 'confirmed')"


class Patient(Base):
    """患者主记录;display_code 是 UI 可见的脱敏编号,不承载身份信息。"""

    __tablename__ = "patients"
    __table_args__ = (
        CheckConstraint(PATIENT_STATUS_SQL, name="ck_patients_status"),
        CheckConstraint(PATIENT_CATEGORY_SQL, name="ck_patients_category_text"),
    )

    id = Column(String(64), primary_key=True, comment="患者 UUID")
    owner_uid = Column(
        String(64),
        ForeignKey("users.uid", ondelete="CASCADE", name="fk_patients_uid_users"),
        nullable=False,
        index=True,
        comment="首期患者资源 Owner",
    )
    display_code = Column(String(64), nullable=False, unique=True, comment="UI 可见的脱敏编号")
    category = Column(String(32), nullable=True, comment="患者癌种分类;未知历史记录为空")
    status = Column(String(20), nullable=False, default="active", server_default="active", index=True)
    identity_fingerprint = Column(
        String(128),
        nullable=True,
        comment="稳定患者标识加盐哈希;脱敏资料预计为空;不写日志或向量",
    )
    current_snapshot_id = Column(String(64), nullable=True, comment="当前已发布快照;由快照发布事务维护")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, server_default=func.now(), nullable=False
    )

    def to_dict(self) -> dict[str, Any]:
        """序列化患者公开字段,不暴露身份指纹。"""
        return {
            "id": self.id,
            "owner_uid": self.owner_uid,
            "display_code": self.display_code,
            "category": self.category,
            "status": self.status,
            "has_identity_fingerprint": self.identity_fingerprint is not None,
            "current_snapshot_id": self.current_snapshot_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class PatientAccessGrant(Base):
    """患者协作授权;可见性查询必须以 Owner 或有效 grant 为条件。"""

    __tablename__ = "patient_access_grants"
    __table_args__ = (
        UniqueConstraint("patient_id", "grantee_uid", name="uq_patient_access_grants_patient_grantee"),
        CheckConstraint("access_level IN ('reader', 'writer')", name="ck_patient_access_grants_level"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_access_grants_patient_patients"),
        nullable=False,
        index=True,
    )
    grantee_uid = Column(
        String(64),
        ForeignKey("users.uid", ondelete="CASCADE", name="fk_patient_access_grants_grantee_users"),
        nullable=False,
        index=True,
    )
    access_level = Column(String(20), nullable=False, comment="reader/writer")
    granted_by = Column(String(64), nullable=False, comment="授权者 UID")
    expires_at = Column(DateTime, nullable=True, comment="过期时间;为空表示长期有效")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class Encounter(Base):
    """患者一次门诊或住院阶段;就诊归属必须有原文依据,未知保持未归属。"""

    __tablename__ = "encounters"
    __table_args__ = (
        UniqueConstraint("patient_id", "external_visit_key", name="uq_encounters_patient_visit_key"),
        CheckConstraint("status IN ('active', 'closed')", name="ck_encounters_status"),
    )

    id = Column(String(64), primary_key=True, comment="就诊 UUID")
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_encounters_patient_patients"),
        nullable=False,
        index=True,
    )
    external_visit_key = Column(String(128), nullable=True, comment="院内就诊号或等效外部键;未知为空")
    encounter_type = Column(String(32), nullable=False, comment="inpatient/outpatient/other")
    started_at = Column(DateTime, nullable=True, comment="入诊时间;以原文为准")
    ended_at = Column(DateTime, nullable=True, comment="出诊时间;以原文为准")
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class PatientImportBatch(Base):
    """一次病历导入的批次状态机与归属确认记录;线程上传与种子导入共用。"""

    __tablename__ = "patient_import_batches"
    __table_args__ = (
        CheckConstraint(PATIENT_BATCH_STATUS_SQL, name="ck_patient_import_batches_status"),
        CheckConstraint(PATIENT_IDENTITY_STATUS_SQL, name="ck_patient_import_batches_identity_status"),
        CheckConstraint(PATIENT_ASSIGNMENT_STATUS_SQL, name="ck_patient_import_batches_assignment_status"),
        CheckConstraint(
            "source_kind IN ('thread_upload', 'seed')", name="ck_patient_import_batches_source_kind"
        ),
        Index("ix_patient_import_batches_patient_status", "patient_id", "status"),
    )

    id = Column(String(64), primary_key=True, comment="批次 UUID")
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_import_batches_patient_patients"),
        nullable=False,
        index=True,
    )
    conversation_id = Column(Integer, nullable=True, comment="发起会话;种子导入为空")
    visit_id = Column(
        String(64),
        ForeignKey("encounters.id", ondelete="SET NULL", name="fk_patient_import_batches_visit_encounters"),
        nullable=True,
    )
    source_kind = Column(String(20), nullable=False, comment="thread_upload/seed")
    dataset_key = Column(String(128), nullable=True, comment="种子数据集键;线程上传为空")
    manifest_hash = Column(String(128), nullable=True, comment="种子清单哈希;归属确认与重放的锚点")
    idempotency_key = Column(String(128), nullable=True, index=True, comment="导入意图幂等键")
    status = Column(String(32), nullable=False, default="uploaded", server_default="uploaded", index=True)
    identity_status = Column(String(32), nullable=False, default="pending", server_default="pending")
    assignment_status = Column(String(32), nullable=False, default="pending", server_default="pending")
    assignment_method = Column(String(32), nullable=True, comment="manual/manifest_review")
    assigned_by = Column(String(64), nullable=True, comment="归属确认者 UID")
    assigned_at = Column(DateTime, nullable=True)
    task_id = Column(String(64), nullable=True, comment="Durable Task ID")
    requested_by = Column(String(64), nullable=False, comment="发起者 UID")
    error_code = Column(String(64), nullable=True)
    published_snapshot_id = Column(String(64), nullable=True, comment="发布成功后的快照;由发布事务回填")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=utc_now_naive, onupdate=utc_now_naive, server_default=func.now(), nullable=False
    )


class PatientDocument(Base):
    """逻辑病历文档;不保存内容,内容在不可变版本中。"""

    __tablename__ = "patient_documents"
    __table_args__ = (
        UniqueConstraint("patient_id", "logical_key", name="uq_patient_documents_patient_logical_key"),
        CheckConstraint("status IN ('active', 'deleted')", name="ck_patient_documents_status"),
    )

    id = Column(String(64), primary_key=True, comment="逻辑文档 UUID")
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_documents_patient_patients"),
        nullable=False,
        index=True,
    )
    visit_id = Column(
        String(64),
        ForeignKey("encounters.id", ondelete="SET NULL", name="fk_patient_documents_visit_encounters"),
        nullable=True,
        comment="就诊归属;依据不足时为空(unassigned)",
    )
    document_type = Column(String(64), nullable=False, comment="文书类型")
    logical_key = Column(String(255), nullable=False, comment="受控字段生成的稳定文档键")
    event_started_at = Column(DateTime, nullable=True, comment="文书事件时间;保留原文精度")
    event_ended_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class PatientDocumentVersion(Base):
    """一次上传的不可变原件版本;已发布后不可覆盖,纠错生成新版本。"""

    __tablename__ = "patient_document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_patient_document_versions_doc_version"),
        CheckConstraint("status IN ('active', 'superseded')", name="ck_patient_document_versions_status"),
        Index("ix_patient_document_versions_patient_hash", "patient_id", "content_hash"),
    )

    id = Column(String(64), primary_key=True, comment="版本 UUID")
    document_id = Column(
        String(64),
        ForeignKey("patient_documents.id", ondelete="CASCADE", name="fk_patient_document_versions_doc_documents"),
        nullable=False,
        index=True,
    )
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_document_versions_patient_patients"),
        nullable=False,
        comment="冗余归属;与 document.patient_id 一致,由 ingest service 保证",
    )
    version = Column(Integer, nullable=False, comment="逻辑文档内单调递增版本号")
    import_batch_id = Column(
        String(64),
        ForeignKey("patient_import_batches.id", ondelete="SET NULL", name="fk_patient_document_versions_batch"),
        nullable=True,
    )
    content_hash = Column(String(128), nullable=False, comment="原件 SHA-256")
    original_object_key = Column(String(512), nullable=False, comment="MinIO 对象键;只含 UUID 不含身份")
    mime_type = Column(String(128), nullable=True)
    size = Column(BigInteger, nullable=True)
    source_system = Column(String(64), nullable=True, comment="来源系统标识")
    effective_at = Column(DateTime, nullable=True, comment="资料生效时间;未知为空")
    uploaded_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)
    supersedes_version_id = Column(String(64), nullable=True, comment="被替换的历史版本")
    status = Column(String(20), nullable=False, default="active", server_default="active")


class PatientDocumentRevision(Base):
    """患者文档版本的解析修订;审核发布后不可变,与原件版本分列计数。"""

    __tablename__ = "patient_document_revisions"
    __table_args__ = (
        UniqueConstraint("document_version_id", "version", name="uq_patient_document_revisions_version"),
        CheckConstraint(
            "status IN ('processing', 'review_required', 'approved', 'rejected')",
            name="ck_patient_document_revisions_status",
        ),
    )

    id = Column(String(64), primary_key=True, comment="解析修订 UUID")
    document_version_id = Column(
        String(64),
        ForeignKey("patient_document_versions.id", ondelete="CASCADE", name="fk_patient_doc_revisions_version"),
        nullable=False,
        index=True,
    )
    version = Column(Integer, nullable=False, comment="解析修订号;与原件版本分列")
    raw_content = Column(Text, nullable=True, comment="解析原始输出")
    content = Column(Text, nullable=True, comment="人工审核后的结构化内容")
    structure_report = Column(JSON_VALUE, nullable=True, comment="逐页结构与异常报告")
    parser_id = Column(String(64), nullable=True)
    parser_version = Column(String(64), nullable=True)
    status = Column(String(20), nullable=False, default="processing", server_default="processing")
    approved_by = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    indexed_at = Column(DateTime, nullable=True, comment="文块与向量写入完成时间")
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class PatientChunk(Base):
    """患者文块 PG 事实;chunk_id 与 Milvus 投影同键,回读校验以此为准。"""

    __tablename__ = "patient_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "revision_version", "chunk_index", name="uq_patient_chunks_loc"),
        Index("ix_patient_chunks_patient_version", "patient_id", "document_version_id"),
    )

    chunk_id = Column(String(64), primary_key=True, comment="文块 UUID;与 Milvus 主键一致")
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_chunks_patient_patients"),
        nullable=False,
    )
    visit_id = Column(String(64), nullable=True, comment="冗余就诊归属;与所属文档一致")
    document_id = Column(String(64), nullable=False)
    document_version_id = Column(
        String(64),
        ForeignKey("patient_document_versions.id", ondelete="CASCADE", name="fk_patient_chunks_version"),
        nullable=False,
    )
    revision_version = Column(Integer, nullable=False, comment="产生文块的解析修订号")
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    char_start = Column(Integer, nullable=True)
    char_end = Column(Integer, nullable=True)
    page_number = Column(Integer, nullable=True, comment="原文页码;引用定位依据")
    coordinates = Column(JSON_VALUE, nullable=True)
    event_time = Column(DateTime, nullable=True, comment="文块事件时间;保留原文精度")
    document_type = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class PatientSnapshot(Base):
    """患者时点快照;旧快照与成员永不修改,新版本走复制-替换发布。"""

    __tablename__ = "patient_snapshots"
    __table_args__ = (
        UniqueConstraint("patient_id", "sequence", name="uq_patient_snapshots_patient_sequence"),
        CheckConstraint("status IN ('published', 'superseded')", name="ck_patient_snapshots_status"),
    )

    id = Column(String(64), primary_key=True, comment="快照 UUID")
    patient_id = Column(
        String(64),
        ForeignKey("patients.id", ondelete="CASCADE", name="fk_patient_snapshots_patient_patients"),
        nullable=False,
        index=True,
    )
    sequence = Column(Integer, nullable=False, comment="患者内单调递增快照序号")
    status = Column(String(20), nullable=False, default="published", server_default="published")
    manifest_hash = Column(String(128), nullable=False, comment="成员集合哈希;防篡改锚点")
    created_by_batch_id = Column(String(64), nullable=True, comment="触发发布的批次")
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utc_now_naive, server_default=func.now(), nullable=False)


class PatientSnapshotMember(Base):
    """快照成员逐条固定版本与解析修订;同一原件的新 OCR 修订不混入旧快照。"""

    __tablename__ = "patient_snapshot_members"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "document_id", name="uq_patient_snapshot_members_snapshot_doc"),
        Index("ix_patient_snapshot_members_version", "document_version_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(
        String(64),
        ForeignKey("patient_snapshots.id", ondelete="CASCADE", name="fk_patient_snapshot_members_snapshot"),
        nullable=False,
        index=True,
    )
    document_id = Column(String(64), nullable=False, comment="逻辑文档;同快照内唯一")
    document_version_id = Column(
        String(64),
        ForeignKey("patient_document_versions.id", ondelete="CASCADE", name="fk_patient_snapshot_members_version"),
        nullable=False,
    )
    document_revision_id = Column(
        String(64),
        ForeignKey("patient_document_revisions.id", ondelete="CASCADE", name="fk_patient_snapshot_members_revision"),
        nullable=False,
        comment="入选快照的解析修订",
    )
    index_generation = Column(Integer, nullable=False, default=1, comment="向量索引代;重建投影时递增")


def patient_fk_definition() -> str:
    """返回 conversations.patient_id 外键约束的名称,供迁移 DO 块与测试共用。"""
    return "fk_conversations_patient_id_patients"


__all__ = [
    "PATIENT_STATUS_SQL",
    "PATIENT_BATCH_SOURCE_KINDS",
    "PATIENT_BATCH_ACTIVE_STATUSES",
    "PATIENT_BATCH_TERMINAL_STATUSES",
    "Encounter",
    "Patient",
    "PatientAccessGrant",
    "PatientChunk",
    "PatientDocument",
    "PatientDocumentRevision",
    "PatientDocumentVersion",
    "PatientImportBatch",
    "PatientSnapshot",
    "PatientSnapshotMember",
    "patient_fk_definition",
]
