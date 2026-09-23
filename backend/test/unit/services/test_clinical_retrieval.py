"""阶段 4 临床检索单元测试:作用域解析、跨快照拒绝、引用验证与工具契约(aiosqlite)。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.repositories.patient_repository import PatientRepository
from yuxi.services import clinical_retrieval_service
from yuxi.services.clinical_retrieval_service import (
    read_patient_evidence_view,
    resolve_patient_scope,
    search_patient_records_view,
    verify_patient_citation,
)
from yuxi.storage.postgres.models_business import Base
from yuxi.storage.postgres.models_clinical import (
    Patient,
    PatientChunk,
    PatientDocument,
    PatientDocumentRevision,
    PatientDocumentVersion,
    PatientSnapshot,
    PatientSnapshotMember,
)

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


async def _seed_world(db, *, with_thread=True):
    await PatientRepository(db).add(
        Patient(id="p-a", owner_uid="doctor-1", display_code="P-A0000001", status="active",
                current_snapshot_id="snap-a1")
    )
    await PatientRepository(db).add(
        Patient(id="p-b", owner_uid="doctor-1", display_code="P-B0000001", status="active")
    )
    if with_thread:
        await ConversationRepository(db).add_conversation(
            uid="doctor-1",
            agent_id="clinical",
            thread_id="thread-a",
            project_id="proj-1",
            patient_id="p-a",
        )
    document = PatientDocument(
        id="doc-a", patient_id="p-a", document_type="medical_record", logical_key="k1", status="active"
    )
    db.add(document)
    await db.flush()
    version = PatientDocumentVersion(
        id="ver-a1", document_id="doc-a", patient_id="p-a", version=1,
        content_hash="a" * 64, original_object_key="clinical/a/v1.pdf", status="active",
    )
    version_b = PatientDocumentVersion(
        id="ver-b1", document_id="doc-a", patient_id="p-a", version=2,
        content_hash="b" * 64, original_object_key="clinical/a/v2.pdf", status="active",
    )
    db.add_all([version, version_b])
    await db.flush()
    revision = PatientDocumentRevision(
        id="rev-a1", document_version_id="ver-a1", version=1, content="患者事实", status="approved"
    )
    db.add(revision)
    await db.flush()
    db.add_all(
        [
            PatientChunk(chunk_id="chunk-in", patient_id="p-a", document_id="doc-a",
                         document_version_id="ver-a1", revision_version=1, chunk_index=0, content="患者事实"),
            PatientChunk(chunk_id="chunk-out", patient_id="p-a", document_id="doc-a",
                         document_version_id="ver-b1", revision_version=1, chunk_index=0, content="快照外内容"),
        ]
    )
    db.add_all(
        [
            PatientSnapshot(id="snap-a1", patient_id="p-a", sequence=1, status="published",
                            manifest_hash="h1", published_at=None),
            PatientSnapshot(id="snap-b9", patient_id="p-b", sequence=1, status="published",
                            manifest_hash="h9", published_at=None),
        ]
    )
    db.add(PatientSnapshotMember(snapshot_id="snap-a1", document_id="doc-a",
                                 document_version_id="ver-a1", document_revision_id="rev-a1", index_generation=1))
    await db.commit()


async def test_resolve_scope_rejects_unbound_thread(session):
    await _seed_world(session, with_thread=False)
    with pytest.raises(HTTPException) as exc:
        await resolve_patient_scope(session, thread_id="thread-a", uid="doctor-1")
    assert exc.value.status_code == 404


async def test_resolve_scope_rejects_other_users_thread(session):
    await _seed_world(session)
    with pytest.raises(HTTPException) as exc:
        await resolve_patient_scope(session, thread_id="thread-a", uid="intruder")
    assert exc.value.status_code == 404


async def test_resolve_scope_rejects_foreign_snapshot(session):
    await _seed_world(session)
    with pytest.raises(HTTPException) as exc:
        await resolve_patient_scope(session, thread_id="thread-a", uid="doctor-1", snapshot_id="snap-b9")
    assert exc.value.status_code == 409


async def test_resolve_scope_defaults_to_current_snapshot(session):
    await _seed_world(session)
    scope = await resolve_patient_scope(session, thread_id="thread-a", uid="doctor-1")
    assert scope["patient"].id == "p-a"
    assert scope["snapshot"].id == "snap-a1"


async def test_evidence_read_rejects_out_of_snapshot_chunk(session):
    await _seed_world(session)
    evidence = await read_patient_evidence_view(session, thread_id="thread-a", uid="doctor-1", chunk_id="chunk-in")
    assert evidence["content"] == "患者事实"
    assert evidence["snapshot_id"] == "snap-a1"

    with pytest.raises(HTTPException) as exc:
        await read_patient_evidence_view(session, thread_id="thread-a", uid="doctor-1", chunk_id="chunk-out")
    assert exc.value.status_code == 404


async def test_citation_verification_rejects_forged_chunks(session):
    await _seed_world(session)
    result = await verify_patient_citation(
        session, patient_id="p-a", snapshot_id="snap-a1", cited_chunk_ids=["chunk-in", "chunk-out", "forged"]
    )
    assert result["accepted"] == ["chunk-in"]
    assert set(result["rejected"]) == {"chunk-out", "forged"}


class _FakeEmbedModel:
    def __init__(self, vector):
        self._vector = vector

    async def abatch_encode(self, texts, batch_size=1):
        return [list(self._vector)]


async def test_embed_query_l2_normalizes(monkeypatch):
    monkeypatch.setenv(clinical_retrieval_service.PATIENT_EMBEDDING_SPEC_ENV, "fake:model")
    import yuxi.models.embed as embed_module

    monkeypatch.setattr(embed_module, "select_embedding_model", lambda spec: _FakeEmbedModel([3.0, 4.0]))
    vector = await clinical_retrieval_service._embed_query("症状")
    assert vector == [0.6, 0.8]


async def test_embed_query_rejects_zero_vector(monkeypatch):
    monkeypatch.setenv(clinical_retrieval_service.PATIENT_EMBEDDING_SPEC_ENV, "fake:model")
    import yuxi.models.embed as embed_module

    monkeypatch.setattr(embed_module, "select_embedding_model", lambda spec: _FakeEmbedModel([0.0, 0.0]))
    with pytest.raises(HTTPException) as exc:
        await clinical_retrieval_service._embed_query("症状")
    assert exc.value.status_code == 503


async def test_search_drops_cross_patient_and_out_of_snapshot_hits(session, monkeypatch):
    await _seed_world(session)

    async def _fake_embed(query_text):
        return [0.1, 0.2]

    async def _fake_search(*, patient_id, snapshot_members, query_embedding, top_k, **kwargs):
        return [
            {"chunk_id": "chunk-in", "score": 0.9},
            {"chunk_id": "chunk-out", "score": 0.8},
            {"chunk_id": "chunk-foreign", "score": 0.99},
        ]

    monkeypatch.setattr(clinical_retrieval_service, "_embed_query", _fake_embed)
    import yuxi.knowledge.patient_index as patient_index

    monkeypatch.setattr(patient_index, "search_patient_chunks", _fake_search)

    hits = await search_patient_records_view(session, thread_id="thread-a", uid="doctor-1", query_text="症状")
    assert [hit["chunk_id"] for hit in hits] == ["chunk-in"]
    assert hits[0]["document_name"] == "k1"
    assert hits[0]["score"] == 0.9


async def test_clinical_tool_schemas_expose_no_scope_parameters():
    from yuxi.agents.toolkits.clinical.tools import get_clinical_tools

    for tool_obj in get_clinical_tools():
        fields = set(tool_obj.args_schema.model_fields)
        assert "patient_id" not in fields, f"{tool_obj.name} 泄漏 patient_id 参数"
        assert "snapshot_id" not in fields, f"{tool_obj.name} 泄漏 snapshot_id 参数"
