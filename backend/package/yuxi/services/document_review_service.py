"""清洗稿准备、人工修订与审核用例。"""

from yuxi.knowledge.cleaning import clean_document
from yuxi.knowledge.utils import parse_minio_url
from yuxi.repositories.document_review_repository import DocumentReviewRepository, ReviewConflict
from yuxi.storage.minio import get_minio_client


async def read_review(kb_id, file_id):
    """读取审核资料，不因查看而创建版本。"""
    result = await DocumentReviewRepository().read(kb_id, file_id)
    result.pop("markdown_file", None)
    return result


async def change_review(kb_id, file_id, *, action, version, operator, content=None):
    """保留旧解析原文并按版本条件执行管理操作。"""
    repo = DocumentReviewRepository()
    initial = None
    if action == "prepare":
        data = await repo.read(kb_id, file_id)
        if not data["markdown_file"]:
            raise ReviewConflict("请先解析文件")
        bucket, key = parse_minio_url(data["markdown_file"])
        raw = (await get_minio_client().adownload_file(bucket, key)).decode("utf-8")
        cleaned, report = clean_document(raw)
        initial = (raw, cleaned, report)
    await repo.write(
        kb_id,
        file_id,
        version=version,
        operator=operator,
        content=content,
        initial=initial,
        approve=action == "approve",
    )
    return await read_review(kb_id, file_id)
