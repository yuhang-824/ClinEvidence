"""阶段 3 快照发布集成测试:成员复制-替换、原子发布与未审核门禁(真实 PostgreSQL)。"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.patient_import_repository import PatientImportRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.services.patient_snapshot_service import (
    approve_revision,
    build_and_store_chunks,
    build_chunks_for_revision,
    publish_snapshot,
)
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import Base, User
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

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    yield


def _scoped_manager(engine) -> PostgresManager:
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


@pytest_asyncio.fixture()
async def clinical_db():
    schema = f"pytest_snapshot_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    manager = _scoped_manager(scoped_engine)
    await manager.create_business_tables()
    await manager.ensure_business_schema()
    factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await scoped_engine.dispose()
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await admin_engine.dispose()


async def _seed_owner(db):
    db.add(User(uid="snap-doctor", username="snap-doctor", password_hash="x", role="user"))
    await db.commit()


async def _seed_patient_with_batch(db, *, batch_status="review_required"):
    patient = Patient(id="snap-patient", owner_uid="snap-doctor", display_code="P-SNAP0001", status="active")
    await PatientRepository(db).add(patient)
    batch = PatientImportBatch(
        id="snap-batch",
        patient_id="snap-patient",
        source_kind="thread_upload",
        status=batch_status,
        identity_status="identity_unverified",
        assignment_status="confirmed",
        requested_by="snap-doctor",
    )
    await PatientImportRepository(db).add_batch(batch)
    await db.commit()
    return patient, batch


async def _seed_version_and_revision(db, *, approved=True, content="第一段\n第二段\n", version_status="active"):
    document = PatientDocument(
        id="snap-doc",
        patient_id="snap-patient",
        document_type="medical_record",
        logical_key="seed:doc1",
        status="active",
    )
    db.add(document)
    version = PatientDocumentVersion(
        id="snap-version",
        document_id=document.id,
        patient_id="snap-patient",
        version=1,
        content_hash="c" * 64,
        original_object_key="clinical/snap/v1.pdf",
        import_batch_id="snap-batch",
        status=version_status,
    )
    db.add(version)
    await db.commit()
    revision = PatientDocumentRevision(
        id="snap-revision",
        document_version_id=version.id,
        version=1,
        content=content,
        status="approved" if approved else "review_required",
    )
    db.add(revision)
    await db.commit()
    return version, revision


def test_chunking_is_deterministic_and_covers_all_text():
    text = "\n".join(f"line-{i} " + "字" * 40 for i in range(60))
    chunks = build_chunks_for_revision(text)
    assert [chunk["index"] for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(text)
    for previous, current in zip(chunks, chunks[1:]):
        assert current["char_start"] == previous["char_end"]


def test_chunking_keeps_section_titles_complete():
    """小标题正文跨块时,每个子块必须重复携带标题;病程时间头与内容绑定同块。"""
    body = "\n".join(f"{i}.患女某岁,既往记录条目内容详述第{i}条。" for i in range(1, 120))
    text = "## 病例特点:\n" + body + "\n## 病程记录\n2025-02-2516:10首次病程记录\n体温正常,术区敷料干燥。"

    chunks = build_chunks_for_revision(text)
    assert len(chunks) > 1
    body_chunks = [c for c in chunks if "既往记录条目内容详述" in c["content"]]
    assert body_chunks, "病例特点正文丢失"
    assert all(c["content"].startswith("## 病例特点") for c in body_chunks)
    course = [c for c in chunks if "首次病程记录" in c["content"]]
    assert len(course) == 1 and "体温正常" in course[0]["content"]


async def test_publish_requires_approved_revision_and_chunks(clinical_db):
    await _seed_owner(clinical_db)
    await _seed_patient_with_batch(clinical_db)
    await _seed_version_and_revision(clinical_db, approved=False)

    with pytest.raises(HTTPException) as exc:
        await publish_snapshot(batch_id="snap-batch", current_uid="snap-doctor", db=clinical_db)
    assert exc.value.status_code == 409


async def test_full_publish_creates_snapshot_and_supersedes_previous(clinical_db, monkeypatch):
    monkeypatch.setenv("YUXI_PATIENT_INDEX_SKIP_VERIFY", "1")
    await _seed_owner(clinical_db)
    await _seed_patient_with_batch(clinical_db)
    version, revision = await _seed_version_and_revision(clinical_db, approved=False)

    await approve_revision(revision_id="snap-revision", current_uid="snap-doctor", db=clinical_db)
    built = await build_and_store_chunks(revision_id="snap-revision", current_uid="snap-doctor", db=clinical_db)
    assert built["status"] == "built"

    result = await publish_snapshot(batch_id="snap-batch", current_uid="snap-doctor", db=clinical_db)
    assert result["status"] == "published"
    snapshot_id = result["snapshot_id"]

    snapshot = await clinical_db.get(PatientSnapshot, snapshot_id)
    assert snapshot.sequence == 1 and snapshot.status == "published"
    members = (
        await clinical_db.execute(
            select(PatientSnapshotMember).where(PatientSnapshotMember.snapshot_id == snapshot_id)
        )
    ).scalars().all()
    assert len(members) == 1
    assert members[0].document_version_id == version.id

    # 纠错版本形成第二快照;旧成员保留在旧快照中可回读。
    correction = PatientDocumentVersion(
        id="snap-version-2",
        document_id=version.document_id,
        patient_id="snap-patient",
        version=2,
        content_hash="d" * 64,
        original_object_key="clinical/snap/v2.pdf",
        import_batch_id="snap-batch",
        status="active",
    )
    clinical_db.add(correction)
    await clinical_db.commit()
    correction_revision = PatientDocumentRevision(
        id="snap-revision-2",
        document_version_id=correction.id,
        version=1,
        content="更正后的内容",
        status="approved",
    )
    clinical_db.add(correction_revision)
    await clinical_db.commit()
    clinical_db.add(
        PatientChunk(
            chunk_id="snap-chunk-2",
            patient_id="snap-patient",
            document_id=version.document_id,
            document_version_id=correction.id,
            revision_version=1,
            chunk_index=0,
            content="更正后的内容",
        )
    )
    batch = await clinical_db.get(PatientImportBatch, "snap-batch")
    batch.status = "review_required"
    await clinical_db.commit()

    second = await publish_snapshot(batch_id="snap-batch", current_uid="snap-doctor", db=clinical_db)
    second_snapshot = await clinical_db.get(PatientSnapshot, second["snapshot_id"])
    assert second_snapshot.sequence == 2
    first_snapshot = await clinical_db.get(PatientSnapshot, snapshot_id)
    assert first_snapshot.status == "superseded"
    first_members = (
        await clinical_db.execute(
            select(PatientSnapshotMember).where(PatientSnapshotMember.snapshot_id == snapshot_id)
        )
    ).scalars().all()
    assert len(first_members) == 1 and first_members[0].document_version_id == "snap-version"


async def test_publish_rejects_non_owner(clinical_db):
    await _seed_owner(clinical_db)
    await _seed_patient_with_batch(clinical_db)
    with pytest.raises(HTTPException) as exc:
        await publish_snapshot(batch_id="snap-batch", current_uid="some-one-else", db=clinical_db)
    assert exc.value.status_code == 404


def test_metadata_registered():

    assert {"patient_snapshots", "patient_snapshot_members", "patient_chunks"} <= set(Base.metadata.tables)
