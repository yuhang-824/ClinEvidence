"""文件行锁拥有清洗版本、审核和索引之间的并发边界。"""

from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import KnowledgeDocumentRevision, KnowledgeFile
from yuxi.utils.datetime_utils import utc_now


class ReviewConflict(ValueError):
    """版本或文件状态已变化。"""


async def latest_revision(session, file_id):
    """读取当前最新内容版本。"""
    return await session.scalar(
        select(KnowledgeDocumentRevision)
        .where(KnowledgeDocumentRevision.file_id == file_id)
        .order_by(KnowledgeDocumentRevision.version.desc())
        .limit(1)
    )


async def add_parsed_revision(session, file_id, raw, content, report, operator):
    """在解析终态的文件锁事务内保存原文与待审核稿。"""
    latest = await latest_revision(session, file_id)
    session.add(
        KnowledgeDocumentRevision(
            file_id=file_id,
            version=latest.version + 1 if latest else 1,
            raw_content=raw,
            content=content,
            report=report,
            created_by=operator,
        )
    )


class DocumentReviewRepository:
    """持久化文档修订和审核。"""

    async def read(self, kb_id, file_id):
        """按知识库归属回读原文、当前版本和历史。"""
        async with pg_manager.get_async_session_context() as session:
            file = await session.scalar(
                select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
            )
            if file is None:
                raise LookupError("文件不存在")
            rows = list(
                (
                    await session.scalars(
                        select(KnowledgeDocumentRevision)
                        .where(KnowledgeDocumentRevision.file_id == file_id)
                        .order_by(KnowledgeDocumentRevision.version)
                    )
                ).all()
            )
            return {
                "file_status": file.status,
                "markdown_file": file.markdown_file,
                "revisions": [
                    {
                        "version": r.version,
                        "content": r.content,
                        "raw_content": r.raw_content,
                        "report": r.report,
                        "created_by": r.created_by,
                        "created_at": r.created_at.isoformat(),
                        "indexed_at": r.indexed_at.isoformat() if r.indexed_at else None,
                        "approved_by": r.approved_by,
                        "approved_at": r.approved_at.isoformat() if r.approved_at else None,
                    }
                    for r in rows
                ],
            }

    async def write(self, kb_id, file_id, *, version, operator, content=None, initial=None, approve=False):
        """拒绝陈旧版本和处理中编辑，审核只作用于已保存的最新版本。"""
        async with pg_manager.get_async_session_context() as session:
            file = await session.scalar(
                select(KnowledgeFile)
                .where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
                .with_for_update()
            )
            if file is None:
                raise LookupError("文件不存在")
            if file.status not in {"parsed", "indexed", "error_indexing", "done"} or not file.markdown_file:
                raise ReviewConflict("文件尚未解析完成或正在处理，请稍后重试")
            latest = await latest_revision(session, file_id)
            if (latest.version if latest else 0) != version:
                raise ReviewConflict("文档版本已更新，请重新加载后再操作")
            if initial is not None:
                if latest:
                    raise ReviewConflict("清洗稿已存在")
                raw, cleaned, report = initial
                await add_parsed_revision(session, file_id, raw, cleaned, report, operator)
            elif latest is None:
                raise ReviewConflict("请先生成清洗稿")
            elif approve:
                if not latest.content.strip():
                    raise ReviewConflict("空内容不能审核入库")
                if not latest.approved_at:
                    latest.approved_by, latest.approved_at = operator, utc_now()
            else:
                session.add(
                    KnowledgeDocumentRevision(
                        file_id=file_id,
                        version=version + 1,
                        content=content,
                        report={
                            "version": 1,
                            "changes": [],
                            "warnings": latest.report.get("warnings", []),
                            "manual": True,
                        },
                        created_by=operator,
                    )
                )
            file.updated_by, file.updated_at = operator, utc_now()

    async def approved_content(self, kb_id, file_id):
        """索引持有文件处理权期间读取最新已审核内容。"""
        async with pg_manager.get_async_session_context() as session:
            file = await session.scalar(
                select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
            )
            revision = await latest_revision(session, file_id) if file else None
            if revision is None or not revision.approved_at:
                raise ReviewConflict("请先审核最新清洗稿，再执行入库")
            return revision.content

    async def published_content(self, kb_id, file_id):
        """模型只能读取成功入库的版本，旧已入库文件保留原文读取路径。"""
        async with pg_manager.get_async_session_context() as session:
            file = await session.scalar(
                select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
            )
            if file is None:
                raise LookupError("文件不存在")
            revision = await session.scalar(
                select(KnowledgeDocumentRevision)
                .where(KnowledgeDocumentRevision.file_id == file_id, KnowledgeDocumentRevision.indexed_at.is_not(None))
                .order_by(KnowledgeDocumentRevision.indexed_at.desc())
                .limit(1)
            )
            if revision is not None:
                return revision.content
            if file.status in {"indexed", "done"}:
                return None
            raise ReviewConflict("文件尚未审核入库，不能作为医助知识内容读取")
