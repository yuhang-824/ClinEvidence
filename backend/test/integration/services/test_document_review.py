"""真实 PostgreSQL 验证审核、索引与编辑的文件锁边界。"""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from test.integration.services.test_schema_migration_version import _create_isolated_manager, _drop_isolated_schema
from yuxi.repositories import document_review_repository, knowledge_file_repository
from yuxi.repositories.document_review_repository import DocumentReviewRepository, ReviewConflict
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeDocumentRevision, KnowledgeFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


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
