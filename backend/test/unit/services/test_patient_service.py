"""阶段 1 患者域单元测试:患者用例、可见性与会话绑定约束(aiosqlite)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.services.conversation_service import _require_matching_thread_creation_intent
from yuxi.services.patient_service import (
    create_patient_view,
    delete_patient_view,
    get_patient_view,
    require_patient_for_thread_binding,
    update_patient_view,
)
from yuxi.storage.postgres.models_business import Base

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


async def _create_patient(db, *, uid="doctor-1", display_code=None):
    return await create_patient_view(
        current_uid=uid,
        display_code=display_code,
        db=db,
    )


async def test_create_patient_defaults_owner_and_generated_code(session):
    patient = await _create_patient(session, uid="doctor-1")

    assert patient["owner_uid"] == "doctor-1"
    assert patient["display_code"].startswith("P-") and len(patient["display_code"]) == 10
    assert patient["status"] == "active"
    assert patient["current_snapshot_id"] is None
    assert "identity_fingerprint" not in patient


async def test_create_patient_rejects_duplicate_display_code(session):
    await _create_patient(session, display_code="P-FIXED-001")

    with pytest.raises(HTTPException) as exc:
        await _create_patient(session, display_code="P-FIXED-001")
    assert exc.value.status_code == 409


async def test_patient_invisible_to_other_users_and_visible_after_grant(session):
    patient = await _create_patient(session, uid="doctor-1")

    with pytest.raises(HTTPException) as exc:
        await get_patient_view(patient_id=patient["id"], current_uid="doctor-2", db=session)
    assert exc.value.status_code == 404

    from yuxi.storage.postgres.models_clinical import PatientAccessGrant

    repo = PatientRepository(session)
    await repo.add_grant(
        PatientAccessGrant(
            patient_id=patient["id"],
            grantee_uid="doctor-2",
            access_level="reader",
            granted_by="doctor-1",
        )
    )
    await session.commit()
    assert await repo.get_accessible_active(patient["id"], "doctor-2") is not None


async def test_update_patient_rejects_non_owner_and_invalid_status(session):
    patient = await _create_patient(session, uid="doctor-1")

    # 无授权用户与不存在同形 404,不泄露患者存在性。
    with pytest.raises(HTTPException) as exc:
        await update_patient_view(
            patient_id=patient["id"], current_uid="doctor-2", status="archived", db=session
        )
    assert exc.value.status_code == 404

    # 有 reader 授权的协作者可见但不可维护,必须 403。
    from yuxi.storage.postgres.models_clinical import PatientAccessGrant

    await PatientRepository(session).add_grant(
        PatientAccessGrant(
            patient_id=patient["id"], grantee_uid="reader-1", access_level="reader", granted_by="doctor-1"
        )
    )
    await session.commit()
    with pytest.raises(HTTPException) as exc:
        await update_patient_view(
            patient_id=patient["id"], current_uid="reader-1", status="archived", db=session
        )
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await update_patient_view(
            patient_id=patient["id"], current_uid="doctor-1", status="deleted", db=session
        )
    assert exc.value.status_code == 400

    updated = await update_patient_view(
        patient_id=patient["id"], current_uid="doctor-1", status="archived", db=session
    )
    assert updated["status"] == "archived"


async def test_binding_requires_patient_for_clinical_agent(session):
    clinical_agent = SimpleNamespace(config_json={"requires_patient": True})

    with pytest.raises(HTTPException) as exc:
        await require_patient_for_thread_binding(
            agent=clinical_agent, patient_id=None, current_uid="doctor-1", db=session
        )
    assert exc.value.status_code == 400


async def test_binding_returns_none_for_plain_agent_without_patient(session):
    plain_agent = SimpleNamespace(config_json={})
    assert (
        await require_patient_for_thread_binding(
            agent=plain_agent, patient_id=None, current_uid="doctor-1", db=session
        )
        is None
    )


async def test_binding_rejects_inaccessible_patient(session):
    patient = await _create_patient(session, uid="doctor-1")
    clinical_agent = SimpleNamespace(config_json={"requires_patient": True})

    with pytest.raises(HTTPException) as exc:
        await require_patient_for_thread_binding(
            agent=clinical_agent, patient_id=patient["id"], current_uid="doctor-2", db=session
        )
    assert exc.value.status_code == 404


async def test_binding_locks_active_patient_for_owner(session):
    patient = await _create_patient(session, uid="doctor-1")
    clinical_agent = SimpleNamespace(config_json={"requires_patient": True})

    locked = await require_patient_for_thread_binding(
        agent=clinical_agent, patient_id=patient["id"], current_uid="doctor-1", db=session
    )
    assert locked is not None and locked.id == patient["id"]


async def test_idempotent_replay_with_different_patient_rejected():
    conversation = SimpleNamespace(status="active", project_id="proj-1", agent_id="clinical", patient_id="p-1")
    project = SimpleNamespace(status="active", selection_status="implicit")

    _require_matching_thread_creation_intent(
        conversation, project, agent_slug="clinical", project_id=None, patient_id="p-1"
    )
    with pytest.raises(HTTPException) as exc:
        _require_matching_thread_creation_intent(
            conversation, project, agent_slug="clinical", project_id=None, patient_id="p-2"
        )
    assert exc.value.status_code == 409


async def test_conversation_persists_immutable_patient_binding(session):
    patient = await _create_patient(session, uid="doctor-1")
    await ConversationRepository(session).add_conversation(
        uid="doctor-1",
        agent_id="clinical",
        thread_id="thread-clinical-1",
        project_id="11111111-1111-4111-8111-111111111111",
        patient_id=patient["id"],
    )
    await session.commit()

    stored = await ConversationRepository(session).get_conversation_by_thread_id("thread-clinical-1")
    assert stored.patient_id == patient["id"]


async def test_delete_patient_cleans_case_data_and_soft_deletes(session, monkeypatch):
    """Owner 删除患者:软删行、清空病例数据与向量;非 Owner 不可删。"""
    from yuxi.storage.postgres.models_clinical import (
        Patient,
        PatientChunk,
        PatientDocument,
        PatientDocumentRevision,
        PatientDocumentVersion,
        PatientImportBatch,
        PatientSnapshot,
    )

    removed = []

    async def _fake_delete(chunk_ids):
        removed.extend(chunk_ids)

    import yuxi.knowledge.patient_index as patient_index

    monkeypatch.setattr(patient_index, "delete_orphan_vectors", _fake_delete)

    patient = await _create_patient(session, uid="doctor-1", display_code="P-DEL0001")
    session.add(
        PatientDocument(
            id="del-doc",
            patient_id=patient["id"],
            document_type="medical_record",
            logical_key="del:doc",
            status="active",
        )
    )
    await session.flush()
    session.add(
        PatientImportBatch(
            id="del-batch",
            patient_id=patient["id"],
            source_kind="thread_upload",
            status="published",
            identity_status="matched",
            assignment_status="confirmed",
            requested_by="doctor-1",
        )
    )
    await session.flush()
    session.add(
        PatientDocumentVersion(
            id="del-ver",
            document_id="del-doc",
            patient_id=patient["id"],
            version=1,
            content_hash="a" * 64,
            original_object_key="clinical/del/v1.pdf",
            import_batch_id="del-batch",
            status="active",
        )
    )
    await session.flush()
    session.add(
        PatientDocumentRevision(
            id="del-rev",
            document_version_id="del-ver",
            version=1,
            content="内容",
            status="approved",
        )
    )
    session.add(
        PatientChunk(
            chunk_id="del-chunk-1",
            patient_id=patient["id"],
            document_id="del-doc",
            document_version_id="del-ver",
            revision_version=1,
            chunk_index=0,
            content="内容",
        )
    )
    session.add(
        PatientSnapshot(
            id="del-snap",
            patient_id=patient["id"],
            sequence=1,
            status="published",
            manifest_hash="h",
        )
    )
    await session.commit()

    result = await delete_patient_view(patient_id=patient["id"], current_uid="doctor-1", db=session)
    assert result["status"] == "deleted"
    assert removed == ["del-chunk-1"]

    stored = await session.get(Patient, patient["id"])
    assert stored.status == "deleted" and stored.current_snapshot_id is None
    assert (await session.get(PatientDocument, "del-doc")) is None
    assert (await session.get(PatientDocumentVersion, "del-ver")) is None
    assert (await session.get(PatientDocumentRevision, "del-rev")) is None
    assert (await session.get(PatientChunk, "del-chunk-1")) is None
    assert (await session.get(PatientImportBatch, "del-batch")) is None
    assert (await session.get(PatientSnapshot, "del-snap")) is None


async def test_delete_patient_rejects_non_owner(session):
    patient = await _create_patient(session, uid="doctor-1", display_code="P-DEL0002")
    with pytest.raises(HTTPException) as exc:
        await delete_patient_view(patient_id=patient["id"], current_uid="doctor-2", db=session)
    assert exc.value.status_code == 404
