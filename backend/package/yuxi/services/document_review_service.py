"""清洗稿准备、人工修订与审核用例。"""

from yuxi.knowledge.cleaning import clean_document
from yuxi.knowledge.utils import parse_minio_url
from yuxi.repositories.document_review_repository import DocumentReviewRepository, ReviewConflict
from yuxi.storage.minio import get_minio_client


async def preview_chunks(kb_id, file_id, *, version, chunk_token_num):
    """预览已保存的确定版本，与索引使用同一结构切片器。"""
    import asyncio

    from yuxi.knowledge.chunking.mixed import chunk_mixed

    data = await DocumentReviewRepository().read(kb_id, file_id)
    revision = data["revisions"][-1] if data["revisions"] else None
    if revision is None or revision["version"] != version:
        raise ReviewConflict("文档版本已更新，请重新加载后预览")
    chunks = await asyncio.to_thread(
        chunk_mixed, revision["content"], file_id, "", {"chunk_token_num": chunk_token_num}, revision=revision
    )
    return {
        "version": version,
        "chunks": chunks,
        "params": {
            "chunk_preset_id": "mixed",
            "chunk_parser_config": {"chunk_token_num": chunk_token_num},
            "review_version": version,
        },
    }


async def read_chunk_source(kb_id, file_id, chunk_id, version):
    """回读片段所属版本的原段落。"""
    from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository

    return await KnowledgeChunkRepository().read_source(kb_id, file_id, chunk_id, version)


async def read_review(kb_id, file_id):
    """读取审核资料，不因查看而创建版本。"""
    result = await DocumentReviewRepository().read(kb_id, file_id)
    result.pop("markdown_file", None)
    return result


async def change_review(
    kb_id, file_id, *, action, version, operator, content=None, boundaries=None, structure=None, base_saved_at=None,
):
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
        repair=action == "boundaries",
        boundaries=boundaries,
        structure=structure,
        base_saved_at=base_saved_at,
    )
    return await read_review(kb_id, file_id)


async def read_structure_artifact(kb_id, file_id, version, page=None):
    """只从所属文件版本回读原始结构或渲染源 PDF 页。"""
    import asyncio
    import hashlib

    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    data = await DocumentReviewRepository().read(kb_id, file_id)
    revision = next((r for r in data["revisions"] if r["version"] == version), None)
    structure = (revision or {}).get("report", {}).get("structure")
    if not structure:
        raise LookupError("此版本没有 PDF 结构原件")
    if page is None:
        bucket, key = parse_minio_url(structure["original_json"])
        return await get_minio_client().adownload_file(bucket, key)
    file = await KnowledgeFileRepository().get_by_file_id(file_id)
    if file is None or file.kb_id != kb_id:
        raise LookupError("文件不存在")
    bucket, key = parse_minio_url(file.minio_url or file.path)
    raw = await get_minio_client().adownload_file(bucket, key)
    if hashlib.sha256(raw).hexdigest() != structure["source_sha256"]:
        raise ReviewConflict("源 PDF 与此审核版本不一致")

    def render():
        """限制原页预览尺寸，坐标比例由前端按原页大小计算。"""
        import pymupdf

        with pymupdf.open(stream=raw, filetype="pdf") as document:
            if not 1 <= page <= len(document):
                raise LookupError("页码不存在")
            source_page = document[page - 1]
            scale = min(2, 1600 / max(source_page.rect.width, source_page.rect.height))
            return source_page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")

    return await asyncio.to_thread(render)
