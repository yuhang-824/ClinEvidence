"""原页与结构原件必须绑定所属文件和解析版本。"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pymupdf
import pytest

from yuxi.repositories.document_review_repository import ReviewConflict
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.services import document_review_service as service


@pytest.mark.asyncio
async def test_source_page_json_and_mismatched_original(monkeypatch):
    """真实 PDF 渲染可读，跨库、非法页码与被替换原件不能冒充版本来源。"""
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Original source")
        raw = document.tobytes()
    structure = {
        "original_json": "http://minio:9000/kb-parsed/k/structure/f/v.json",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }
    read = AsyncMock(return_value={"revisions": [{"version": 1, "report": {"structure": structure}}]})
    monkeypatch.setattr(service.DocumentReviewRepository, "read", read)
    file = SimpleNamespace(kb_id="k", minio_url="http://minio:9000/kb-documents/k/f.pdf", path=None)
    monkeypatch.setattr(KnowledgeFileRepository, "get_by_file_id", AsyncMock(return_value=file))
    client = SimpleNamespace(adownload_file=AsyncMock(return_value=raw))
    monkeypatch.setattr(service, "get_minio_client", lambda: client)
    image = await service.read_structure_artifact("k", "f", 1, 1)
    assert image.startswith(b"\x89PNG")
    with pytest.raises(LookupError, match="页码"):
        await service.read_structure_artifact("k", "f", 1, 2)
    with pytest.raises(LookupError, match="没有"):
        await service.read_structure_artifact("k", "f", 2, 1)
    with pytest.raises(LookupError, match="文件"):
        await service.read_structure_artifact("other", "f", 1, 1)
    client.adownload_file.return_value = b"changed"
    with pytest.raises(ReviewConflict, match="不一致"):
        await service.read_structure_artifact("k", "f", 1, 1)
    client.adownload_file.return_value = b'{"schema_name":"DoclingDocument"}'
    assert await service.read_structure_artifact("k", "f", 1) == b'{"schema_name":"DoclingDocument"}'
