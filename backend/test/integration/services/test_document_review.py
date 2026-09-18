"""真实 PostgreSQL 验证审核、索引与编辑的文件锁边界。"""

import asyncio
from datetime import datetime, UTC

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.integration.services.test_schema_migration_version import _create_isolated_manager, _drop_isolated_schema
from yuxi.repositories import document_review_repository, knowledge_file_repository
from yuxi.repositories.document_review_repository import DocumentReviewRepository, ReviewConflict
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeDocumentRevision, KnowledgeFile
from yuxi.knowledge.structure import structure_report
from test.unit.knowledge.test_structured_pdf_review import sample, payload

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_structured_review_blocks_bypass_and_invalidates_approval(monkeypatch):
    """真实事务证明原页未核验不能审批，修订后旧审批和旧预览失效。"""
    schema, admin, engine, manager = await _create_isolated_manager("pytest_structure")
    manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await manager.create_knowledge_tables()
        monkeypatch.setattr(document_review_repository, "pg_manager", manager)
        monkeypatch.setattr(knowledge_file_repository, "pg_manager", manager)
        async with manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id="kb", name="fixture", kb_type="milvus"))
            await session.flush()
            session.add(
                KnowledgeFile(file_id="file", kb_id="kb", filename="f.pdf", status="parsed", markdown_file="original")
            )
        repo, files = DocumentReviewRepository(), KnowledgeFileRepository()
        source = sample()
        content, report = structure_report(source)
        await repo.write("kb", "file", version=0, operator="parser", initial=("original", content, report))
        with pytest.raises(ValueError, match="尚未完成"):
            await repo.write("kb", "file", version=1, operator="editor", approve=True)
        with pytest.raises(ReviewConflict, match="结构审核"):
            await repo.write("kb", "file", version=1, operator="editor", content="bypass")
        await repo.write("kb", "file", version=1, operator="editor", structure=payload(source))
        await repo.write("kb", "file", version=2, operator="editor", approve=True)
        assert (await repo.approved_revision("kb", "file"))["version"] == 2
        changed = payload(source)
        changed["blocks"][1]["text"] = "修改对应关系"
        changed["pages"][0]["checks"]["relationships"] = False
        await repo.write("kb", "file", version=2, operator="editor", structure=changed)
        rows = (await repo.read("kb", "file"))["revisions"]
        assert rows[-1]["approved_at"] is None
        assert rows[0]["raw_content"] == "original"
        assert rows[-1]["report"]["structure"]["blocks"][1]["source_text"] == "immutable"
        with pytest.raises(ReviewConflict, match="审核"):
            await files.update_fields_if_status(
                kb_id="kb", file_id="file", allowed_statuses={"parsed"}, data={"status": "indexing"}
            )
        # 即使审批字段被旧逻辑错误写入，索引入口仍执行结构核验。
        async with manager.get_async_session_context() as session:
            row = await session.scalar(select(KnowledgeDocumentRevision).where(KnowledgeDocumentRevision.version == 3))
            row.approved_at = datetime.now(UTC)
        with pytest.raises(ValueError, match="尚未完成"):
            await files.update_fields_if_status(
                kb_id="kb",
                file_id="file",
                allowed_statuses={"parsed"},
                data={"status": "indexing", "processing_params": {"review_version": 3}},
            )
    finally:
        await _drop_isolated_schema(schema, admin, engine)


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件使用隔离 Schema。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 没有 HTTP 资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 没有沙盒。"""
    yield


async def test_revisions_gate_concurrency_scope_and_migration(monkeypatch):
    """并发修订仅一方成功，未审核不能索引，处理中不能修改。"""
    schema, admin, engine, manager = await _create_isolated_manager("pytest_review")
    manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await manager.create_knowledge_tables()
        # 模拟 v2 没有版本表，升级重复执行不改变原文件。
        async with engine.begin() as connection:
            await connection.run_sync(KnowledgeDocumentRevision.__table__.drop)
        async with manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id="kb", name="fixture", kb_type="milvus"))
            await session.flush()
            session.add(
                KnowledgeFile(
                    file_id="file", kb_id="kb", filename="fixture.pdf", markdown_file="original", status="parsed"
                )
            )
        await manager.create_knowledge_tables()
        await manager.create_knowledge_tables()
        # v3 升级只能增加来源列，原文件及旧片段必须保留。
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE knowledge_chunks DROP COLUMN source_metadata"))
            await connection.execute(
                text(
                    "INSERT INTO knowledge_chunks (chunk_id,file_id,kb_id,chunk_index,content,"
                    "graph_structure_indexed,graph_indexed,graph_extraction_details) "
                    "VALUES ('old','file','kb',0,'legacy content',false,false,'{}')"
                )
            )
        await manager.upgrade_knowledge_schema_v3_to_v4()
        await manager.upgrade_knowledge_schema_v3_to_v4()
        async with engine.begin() as connection:
            row = (
                await connection.execute(
                    text("SELECT content, source_metadata FROM knowledge_chunks WHERE chunk_id='old'")
                )
            ).one()
            assert row.content == "legacy content" and row.source_metadata is None
        monkeypatch.setattr(document_review_repository, "pg_manager", manager)
        monkeypatch.setattr(knowledge_file_repository, "pg_manager", manager)
        repo = DocumentReviewRepository()
        files = KnowledgeFileRepository()
        with pytest.raises(ReviewConflict, match="审核"):
            await files.update_fields_if_status(
                kb_id="kb", file_id="file", allowed_statuses={"parsed"}, data={"status": "indexing"}
            )
        await repo.write(
            "kb", "file", version=0, operator="reviewer", initial=("original", "cleaned", {"warnings": []})
        )
        results = await asyncio.gather(
            *[repo.write("kb", "file", version=1, operator=name, content=name) for name in ("one", "two")],
            return_exceptions=True,
        )
        assert sum(isinstance(r, ReviewConflict) for r in results) == 1
        snapshot = await repo.read("kb", "file")
        assert [r["version"] for r in snapshot["revisions"]] == [1, 2]
        assert snapshot["revisions"][0]["raw_content"] == "original"
        with pytest.raises(LookupError):
            await repo.write("other-kb", "file", version=2, operator="other", approve=True)
        with pytest.raises(ReviewConflict, match="版本"):
            await repo.write("kb", "file", version=1, operator="reviewer", approve=True)
        await repo.write("kb", "file", version=2, operator="reviewer", approve=True)
        with pytest.raises(ReviewConflict, match="预览版本"):
            await files.update_fields_if_status(
                kb_id="kb",
                file_id="file",
                allowed_statuses={"parsed"},
                data={"status": "indexing", "processing_params": {"review_version": 1}},
            )
        await files.update_fields_if_status(
            kb_id="kb", file_id="file", allowed_statuses={"parsed"}, data={"status": "indexing"}
        )
        with pytest.raises(ReviewConflict, match="正在处理"):
            await repo.write("kb", "file", version=2, operator="reviewer", content="late edit")
        assert await repo.approved_content("kb", "file") in {"one", "two"}
        await files.update_fields_if_status(
            kb_id="kb", file_id="file", allowed_statuses={"indexing"}, data={"status": "indexed"}
        )
        await repo.write("kb", "file", version=2, operator="reviewer", content="new draft")
        with pytest.raises(ReviewConflict, match="审核"):
            await files.update_fields_if_status(
                kb_id="kb", file_id="file", allowed_statuses={"indexed"}, data={"status": "indexing"}
            )
        async with manager.get_async_session_context() as session:
            record = await session.scalar(select(KnowledgeFile).where(KnowledgeFile.file_id == "file"))
            assert record.status == "indexed" and record.markdown_file == "original"
        revisions = (await repo.read("kb", "file"))["revisions"]
        assert revisions[1]["indexed_at"] and revisions[2]["approved_at"] is None
        assert await repo.published_content("kb", "file") in {"one", "two"}
        await repo.write("kb", "file", version=3, operator="reviewer", approve=True)
        raced = await asyncio.gather(
            repo.write("kb", "file", version=3, operator="editor", content="racing draft"),
            files.update_fields_if_status(
                kb_id="kb", file_id="file", allowed_statuses={"indexed"}, data={"status": "indexing"}
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ReviewConflict) for result in raced) == 1
        assert await repo.published_content("kb", "file") in {"one", "two"}
        async with manager.get_async_session_context() as session:
            session.add(
                KnowledgeFile(
                    file_id="legacy",
                    kb_id="kb",
                    filename="legacy.pdf",
                    markdown_file="legacy-original",
                    status="indexed",
                )
            )
        assert await repo.published_content("kb", "legacy") is None
        await repo.write(
            "kb", "legacy", version=0, operator="reviewer", initial=("old source", "new draft", {"warnings": []})
        )
        assert await repo.published_content("kb", "legacy") is None
    finally:
        await _drop_isolated_schema(schema, admin, engine)


async def test_replaced_chunk_rejects_old_source_version(monkeypatch):
    """相同片段 ID 被重新入库覆盖后，旧版本来源请求明确失败。"""
    from yuxi.knowledge.chunking.mixed import chunk_mixed
    from yuxi.repositories import knowledge_chunk_repository
    from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository

    schema, admin, engine, manager = await _create_isolated_manager("pytest_chunk_source")
    manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await manager.create_knowledge_tables()
        monkeypatch.setattr(knowledge_chunk_repository, "pg_manager", manager)
        async with manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id="kb", name="fixture", kb_type="milvus"))
            await session.flush()
            session.add(KnowledgeFile(file_id="file", kb_id="kb", filename="fixture.pdf", status="indexed"))
            await session.flush()
            for version in (1, 2):
                session.add(KnowledgeDocumentRevision(file_id="file", version=version, content="同样的内容", report={}))
        repo = KnowledgeChunkRepository()
        first = chunk_mixed("同样的内容", "file", "fixture.pdf", {}, revision={"version": 1})[0]
        await repo.batch_upsert([{**first, "kb_id": "kb"}])
        assert (await repo.read_source("kb", "file", first["id"], 1))["excerpts"][0]["text"] == "同样的内容"
        second = chunk_mixed("同样的内容", "file", "fixture.pdf", {}, revision={"version": 2})[0]
        assert second["id"] == first["id"]
        await repo.batch_upsert([{**second, "kb_id": "kb"}])
        with pytest.raises(ReviewConflict, match="重新入库"):
            await repo.read_source("kb", "file", first["id"], 1)
        assert (await repo.read_source("kb", "file", second["id"], 2))["source_metadata"]["revision"] == 2
        with pytest.raises(LookupError):
            await repo.read_source("other", "file", second["id"], 2)
    finally:
        await _drop_isolated_schema(schema, admin, engine)


async def test_boundary_revision_preserves_pages_resets_approval_and_automatic(monkeypatch):
    """只改边界保留页码，新版本需审核；恢复自动和正文编辑清除人工方案。"""
    schema, admin, engine, manager = await _create_isolated_manager("pytest_boundary_review")
    manager.AsyncSession = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await manager.create_knowledge_tables()
        monkeypatch.setattr(document_review_repository, "pg_manager", manager)
        async with manager.get_async_session_context() as session:
            session.add(KnowledgeBase(kb_id="kb", name="fixture", kb_type="milvus"))
            await session.flush()
            session.add(
                KnowledgeFile(file_id="file", kb_id="kb", filename="f.pdf", status="parsed", markdown_file="original")
            )
        repo = DocumentReviewRepository()
        report = {"page_spans": [{"page": 1, "start": 0, "end": 8}], "warnings": []}
        await repo.write("kb", "file", version=0, operator="editor", initial=("raw", "第一句。第二句。", report))
        await repo.write("kb", "file", version=1, operator="editor", approve=True)
        await repo.write("kb", "file", version=1, operator="editor", repair=True, boundaries=[4])
        versions = (await repo.read("kb", "file"))["revisions"]
        assert versions[-1]["content"] == versions[0]["content"]
        assert versions[-1]["report"]["page_spans"] == report["page_spans"]
        assert versions[-1]["approved_at"] is None
        with pytest.raises(ValueError, match="切点"):
            await repo.write("kb", "file", version=2, operator="editor", repair=True, boundaries=[8])
        assert len((await repo.read("kb", "file"))["revisions"]) == 2
        await repo.write("kb", "file", version=2, operator="editor", repair=True, boundaries=None)
        latest = (await repo.read("kb", "file"))["revisions"][-1]
        assert "chunk_boundaries" not in latest["report"]
        assert latest["report"]["page_spans"] == report["page_spans"]
        await repo.write("kb", "file", version=3, operator="editor", content="修改后正文。")
        assert "page_spans" not in (await repo.read("kb", "file"))["revisions"][-1]["report"]
    finally:
        await _drop_isolated_schema(schema, admin, engine)
