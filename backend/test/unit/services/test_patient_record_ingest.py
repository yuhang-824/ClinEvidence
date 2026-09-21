"""阶段 2 患者导入单元测试:状态机守卫、归属门禁、重复幂等与身份三态(aiosqlite)。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.patient_import_repository import PatientImportRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.services.patient_record_ingest_service import (
    PatientImportBatch,
    cancel_batch,
    confirm_batch_assignment,
    evaluate_identity_status,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_clinical import Patient

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _make_patient(db, *, uid="doctor-1", patient_id="patient-1", fingerprint=None):
    patient = Patient(
        id=patient_id,
        owner_uid=uid,
        display_code="P-TEST0001",
        status="active",
        identity_fingerprint=fingerprint,
    )
    await PatientRepository(db).add(patient)
    await db.commit()
    return patient


async def _make_batch(
    db,
    *,
    batch_id="batch-1",
    status="identity_check",
    identity="identity_unverified",
    assignment="pending",
):
    batch = PatientImportBatch(
        id=batch_id,
        patient_id="patient-1",
        source_kind="thread_upload",
        status=status,
        identity_status=identity,
        assignment_status=assignment,
        requested_by="doctor-1",
    )
    await PatientImportRepository(db).add_batch(batch)
    await db.commit()
    return batch


async def test_transition_rejects_illegal_paths(session):
    batch = await _make_batch(session, status="uploaded")
    repo = PatientImportRepository(session)

    with pytest.raises(ValueError, match="uploaded"):
        await repo.transition_batch(batch, "published")

    await repo.transition_batch(batch, "identity_check")
    with pytest.raises(ValueError):
        await repo.transition_batch(batch, "indexing")


async def test_published_and_conflict_are_absorbing_states(session):
    repo = PatientImportRepository(session)

    published = await _make_batch(session, status="published", identity="matched", assignment="confirmed")
    with pytest.raises(ValueError):
        await repo.transition_batch(published, "parsing")

    conflict = await _make_batch(
        session, batch_id="batch-conflict", status="identity_conflict", identity="identity_conflict"
    )
    with pytest.raises(ValueError):
        await repo.transition_batch(conflict, "parsing")


async def test_gate_requires_matched_or_confirmed_assignment(session):
    unverified = await _make_batch(session, batch_id="batch-unverified")
    assert PatientImportRepository.gate_allows_processing(unverified) is False

    confirmed = await _make_batch(
        session, batch_id="batch-confirmed", status="identity_check", assignment="confirmed"
    )
    confirmed.assignment_status = "confirmed"
    assert PatientImportRepository.gate_allows_processing(confirmed) is True

    matched = await _make_batch(session, batch_id="batch-matched", identity="matched")
    matched.identity_status = "matched"
    assert PatientImportRepository.gate_allows_processing(matched) is True


async def test_confirm_assignment_owner_only_and_rejects_conflict(session):
    await _make_patient(session)
    await _make_batch(session)

    # 非 Owner(也不可见)404;另一 Owner 可见性之外无法确认。
    with pytest.raises(HTTPException) as exc:
        await confirm_batch_assignment(batch_id="batch-1", current_uid="intruder", method="manual", db=session)
    assert exc.value.status_code == 404

    confirmed = await confirm_batch_assignment(
        batch_id="batch-1", current_uid="doctor-1", method="manual", db=session
    )
    assert confirmed["assignment_status"] == "confirmed"
    assert confirmed["identity_status"] == "identity_unverified"

    await _make_batch(
        session, batch_id="batch-conflict", status="identity_conflict", identity="identity_conflict"
    )
    with pytest.raises(HTTPException) as exc:
        await confirm_batch_assignment(batch_id="batch-conflict", current_uid="doctor-1", method="manual", db=session)
    assert exc.value.status_code == 409

    with pytest.raises(HTTPException) as exc:
        await confirm_batch_assignment(batch_id="batch-1", current_uid="doctor-1", method="manual", db=session)
    assert exc.value.status_code == 409


async def test_cancel_batch_rejects_published(session):
    await _make_patient(session)
    await _make_batch(session)
    cancelled = await cancel_batch(batch_id="batch-1", current_uid="doctor-1", db=session)
    assert cancelled["status"] == "cancelled"

    await _make_batch(
        session, batch_id="batch-published", status="published", identity="matched", assignment="confirmed"
    )
    with pytest.raises(HTTPException) as exc:
        await cancel_batch(batch_id="batch-published", current_uid="doctor-1", db=session)
    assert exc.value.status_code == 409


async def test_identity_status_three_states(session, monkeypatch):
    # 脱敏资料无指纹:恒为 unverified。
    assert evaluate_identity_status(None, ["1234567890"]) == "identity_unverified"

    monkeypatch.setenv("YUXI_IDENTITY_HASH_SALT", "s1")
    from yuxi.services.patient_record_ingest_service import hash_identity_value

    fingerprint = hash_identity_value("1234567890", "s1")
    assert evaluate_identity_status(fingerprint, ["0000000000", "1234567890"]) == "matched"
    assert evaluate_identity_status(fingerprint, ["9999999999"]) == "identity_conflict"

    monkeypatch.delenv("YUXI_IDENTITY_HASH_SALT")
    assert evaluate_identity_status(fingerprint, ["1234567890"]) == "identity_unverified"


async def test_duplicate_content_detection_by_hash(session):
    from yuxi.storage.postgres.models_clinical import PatientDocumentVersion

    await _make_patient(session)
    repo = PatientImportRepository(session)
    await repo.add_document_version(
        PatientDocumentVersion(
            id="ver-1",
            document_id="doc-1",
            patient_id="patient-1",
            version=1,
            content_hash="a" * 64,
            original_object_key="clinical/p1/v1.pdf",
            status="active",
        )
    )
    await session.commit()

    found = await repo.find_version_by_content_hash("patient-1", "a" * 64)
    assert found is not None and found.id == "ver-1"
    assert await repo.find_version_by_content_hash("patient-2", "a" * 64) is None
    assert await repo.find_version_by_content_hash("patient-1", "b" * 64) is None
