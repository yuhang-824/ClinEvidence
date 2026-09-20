from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.config.options import ensure_options_in_db, update_option_value
from yuxi.knowledge.parser.capabilities import PARSER_CAPABILITIES
from yuxi.services import ocr_service
from yuxi.storage.postgres.models_business import Base, ModelProvider


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await ensure_options_in_db(session)
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_resolution_uses_database_option(db_session):
    await update_option_value(
        db_session,
        "mineru_ocr_host_opts",
        {"server_url": "http://mineru-config:30001"},
        "tester",
    )

    resolved = await ocr_service.resolve_ocr_task_params({"ocr_engine": "mineru_ocr"}, db_session)

    assert resolved["_ocr_processor_kwargs"] == {"server_url": "http://mineru-config:30001/"}


@pytest.mark.asyncio
async def test_ocr_options_use_parser_metadata(db_session, monkeypatch):
    async def get_options(option, _db=None):
        assert option is ocr_service.system_options
        return {"default_ocr_engine": "rapid_ocr"}

    monkeypatch.setattr(type(ocr_service.system_options), "get", get_options)
    options = await ocr_service.get_ocr_options(db_session)

    assert options == {
        "default_engine": "rapid_ocr",
        "engines": [
            {
                "engine_id": engine_id,
                "service_name": capability.service_name,
                "display_name": capability.display_name,
                "supported_extensions": list(capability.supported_extensions),
            }
            for engine_id, capability in PARSER_CAPABILITIES.items()
        ],
    }


@pytest.mark.asyncio
async def test_deepseek_uses_provider_credentials_without_chat_models(db_session):
    provider = ModelProvider(
        provider_id="siliconflow-cn",
        display_name="SiliconFlow",
        provider_type="openai",
        base_url="https://provider.example/v1",
        is_enabled=True,
        api_key="provider-secret",
        api_key_env=None,
        capabilities=["embedding"],
        enabled_models=[{"id": "BAAI/bge-m3", "type": "embedding"}],
    )
    db_session.add(provider)
    await db_session.flush()

    resolved = await ocr_service.resolve_ocr_task_params({"ocr_engine": "deepseek_ocr"}, db_session)

    assert resolved["_ocr_processor_kwargs"] == {
        "api_key": "provider-secret",
        "api_url": "https://provider.example/v1/chat/completions",
    }


@pytest.mark.asyncio
async def test_health_checks_every_registered_ocr_method(db_session, monkeypatch):
    async def build_kwargs(db, engine_id):
        del db
        return {"engine": engine_id}

    monkeypatch.setattr(ocr_service, "_build_processor_kwargs", build_kwargs)
    monkeypatch.setattr(
        ocr_service.DocumentProcessorFactory,
        "check_health",
        lambda engine_id, **kwargs: {"status": "healthy", "message": kwargs["engine"]},
    )

    health = await ocr_service.check_all_ocr_health(db_session)

    assert set(health) == set(PARSER_CAPABILITIES)
    assert all(result["status"] == "healthy" for result in health.values())


class _FakeMinio:
    """记录结构原件上传，避免测试触达真实 MinIO。"""

    KB_BUCKETS = {"images": "kb-images", "parsed": "kb-parsed"}

    def __init__(self):
        self.uploaded: list[dict] = []

    async def adownload_file(self, bucket, key):
        return b"%PDF-1.4 fake bytes"

    async def aupload_file(self, **kwargs):
        self.uploaded.append(kwargs)
        return type("UploadResult", (), {"url": "http://minio/kb-parsed/structure.json"})()


def _mineru_layout():
    return {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [595, 842],
                "para_blocks": [
                    {
                        "type": "title",
                        "bbox": [70, 84, 230, 103],
                        "level": 1,
                        "index": 0,
                        "lines": [{"spans": [{"content": "1 总则"}]}],
                    }
                ],
                "discarded_blocks": [],
            }
        ]
    }


def _patch_dispatch(monkeypatch, engine_id, minio, keyword_kwargs=None):
    """固定引擎解析结果并隔离 MinIO 与解析器构造，返回记录调用的容器。"""

    async def fake_engine(params=None, db=None):
        return engine_id

    async def fake_kwargs(target_engine, db=None):
        return dict(keyword_kwargs or {})

    monkeypatch.setattr(ocr_service, "resolve_ocr_engine_id_for_params", fake_engine)
    monkeypatch.setattr(ocr_service, "build_ocr_processor_kwargs", fake_kwargs)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: minio)
    monkeypatch.setattr("yuxi.knowledge.utils.kb_utils.parse_minio_url", lambda url: ("kb", "key.pdf"))


@pytest.mark.asyncio
async def test_knowledge_pdf_uses_mineru_structure_when_engine_selected(monkeypatch):
    """引擎选 MinerU Official 时 PDF 走 MinerU 结构适配，而非本地 Docling。"""
    minio = _FakeMinio()
    captured: dict = {}

    class FakeMinerUParser:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def parse_structured_pdf(self, path, params):
            captured["params"] = params
            return {
                "markdown": "# 1 总则",
                "content_list": [],
                "layout": _mineru_layout(),
                "model_version": "mineru-test",
                "origin_pdf": None,
            }

    _patch_dispatch(monkeypatch, "mineru_official", minio, {"api_key": "test-key"})
    monkeypatch.setattr(
        "yuxi.knowledge.parser.mineru_official.MinerUOfficialParser", FakeMinerUParser
    )

    raw, content, report = await ocr_service.parse_knowledge_document(
        "http://minio/knowledgebases/kb_1/upload/a.pdf",
        {"ocr_engine": "mineru_official"},
        kb_id="kb_1",
        file_id="file_1",
    )

    assert raw == "# 1 总则"
    # api_base 未配置时传 None，由解析器回退官方端点
    assert captured["kwargs"] == {"api_key": "test-key", "api_base": None}
    assert captured["params"]["image_prefix"] == "kb_1/kb-images"
    assert report["structure"]["parser"] == "mineru"
    assert report["structure"]["parser_version"] == "mineru-test"
    assert [b["kind"] for b in report["structure"]["blocks"]] == ["heading"]
    assert report["structure"]["original_json"] == "http://minio/kb-parsed/structure.json"
    assert minio.uploaded[0]["object_name"].startswith("kb_1/structure/file_1/")
    assert "1 总则" in content


@pytest.mark.asyncio
async def test_knowledge_pdf_keeps_docling_for_local_engines(monkeypatch):
    """未选 MinerU 时 PDF 仍走本地 Docling，不触发云端解析。"""
    minio = _FakeMinio()
    calls: list[str] = []

    def fake_docling(path):
        calls.append("docling")
        structure = {
            "schema": 1,
            "parser": "docling",
            "pages": [
                {
                    "page": 1,
                    "width": 595.0,
                    "height": 842.0,
                    "checks": {
                        "reading_order": False,
                        "text_complete": False,
                        "relationships": False,
                    },
                    "note": "",
                    "issues": [],
                }
            ],
            "blocks": [
                {
                    "id": "b0",
                    "page": 1,
                    "bbox": [1, 2, 3, 4],
                    "kind": "paragraph",
                    "level": 2,
                    "source_text": "本地解析",
                    "text": "本地解析",
                    "excluded": False,
                    "note": "",
                }
            ],
        }
        return {"schema": 1}, "本地解析", structure

    _patch_dispatch(monkeypatch, "rapid_ocr", minio)
    monkeypatch.setattr("yuxi.knowledge.parser.docling_pdf.parse_structured_pdf", fake_docling)
    monkeypatch.setattr(
        "yuxi.knowledge.parser.mineru_official.MinerUOfficialParser",
        type("Never", (), {"__init__": lambda self, **kw: calls.append("mineru")}),
    )

    raw, content, report = await ocr_service.parse_knowledge_document(
        "http://minio/knowledgebases/kb_1/upload/a.pdf",
        {"ocr_engine": "rapid_ocr"},
        kb_id="kb_1",
        file_id="file_1",
    )

    assert calls == ["docling"]
    assert raw == "本地解析"
    assert report["structure"]["parser"] == "docling"
    assert minio.uploaded[0]["content_type"] == "application/json"


@pytest.mark.asyncio
async def test_mineru_engine_without_credentials_fails_instead_of_falling_back(monkeypatch):
    """选 MinerU 但无凭证时显式失败，不静默回退本地解析。"""
    minio = _FakeMinio()
    docling_calls: list[str] = []
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    _patch_dispatch(monkeypatch, "mineru_official", minio, {})
    monkeypatch.setattr(
        "yuxi.knowledge.parser.docling_pdf.parse_structured_pdf",
        lambda path: docling_calls.append("docling"),
    )

    from yuxi.knowledge.parser.base import DocumentParserException

    with pytest.raises(DocumentParserException, match="MINERU_API_KEY"):
        await ocr_service.parse_knowledge_document(
            "http://minio/knowledgebases/kb_1/upload/a.pdf",
            {"ocr_engine": "mineru_official"},
            kb_id="kb_1",
            file_id="file_1",
        )
    assert docling_calls == []
    assert minio.uploaded == []


@pytest.mark.asyncio
async def test_mineru_text_layer_page_mismatch_records_document_issue(monkeypatch):
    """原页文本层页数不一致时跳过比对并在页面留全文提示，而不是崩溃或静默通过。"""
    import pymupdf

    minio = _FakeMinio()
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    origin_bytes = doc.tobytes()
    doc.close()

    class FakeMinerUParser:
        def __init__(self, **kwargs):
            pass

        def parse_structured_pdf(self, path, params):
            return {
                "markdown": "# 1 总则",
                "content_list": [],
                "layout": _mineru_layout(),
                "model_version": "mineru-test",
                "origin_pdf": origin_bytes,
            }

    _patch_dispatch(monkeypatch, "mineru_official", minio, {"api_key": "test-key"})
    monkeypatch.setattr(
        "yuxi.knowledge.parser.mineru_official.MinerUOfficialParser", FakeMinerUParser
    )

    _raw, _content, report = await ocr_service.parse_knowledge_document(
        "http://minio/knowledgebases/kb_1/upload/a.pdf",
        {"ocr_engine": "mineru_official"},
        kb_id="kb_1",
        file_id="file_1",
    )

    issues = report["structure"]["pages"][0]["issues"]
    assert any("全文：原页文本层 2 页与解析 1 页不一致" in issue for issue in issues)


@pytest.mark.asyncio
async def test_local_pdf_parsing_ignores_unrelated_engine_credentials(monkeypatch):
    """本地引擎的 PDF 解析不因其它引擎凭证缺失而失败（引擎分发只解析标识）。"""
    minio = _FakeMinio()

    async def fake_engine(params=None, db=None):
        # 模拟 deepseek_ocr：其凭证解析会因供应商未配置而抛错
        return "deepseek_ocr"

    async def exploding_kwargs(target_engine, db=None):
        raise ValueError("siliconflow-cn 模型供应商凭证不可用")

    monkeypatch.setattr(ocr_service, "resolve_ocr_engine_id_for_params", fake_engine)
    monkeypatch.setattr(ocr_service, "build_ocr_processor_kwargs", exploding_kwargs)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: minio)
    monkeypatch.setattr("yuxi.knowledge.utils.kb_utils.parse_minio_url", lambda url: ("kb", "key.pdf"))
    monkeypatch.setattr(
        "yuxi.knowledge.parser.docling_pdf.parse_structured_pdf",
        lambda path: (
            {"schema": 1},
            "本地解析",
            {
                "schema": 1,
                "parser": "docling",
                "pages": [
                    {
                        "page": 1,
                        "width": 595.0,
                        "height": 842.0,
                        "checks": {
                            "reading_order": False,
                            "text_complete": False,
                            "relationships": False,
                        },
                        "note": "",
                        "issues": [],
                    }
                ],
                "blocks": [],
            },
        ),
    )

    raw, _content, report = await ocr_service.parse_knowledge_document(
        "http://minio/knowledgebases/kb_1/upload/a.pdf",
        {"ocr_engine": "deepseek_ocr"},
        kb_id="kb_1",
        file_id="file_1",
    )

    assert raw == "本地解析"
    assert report["structure"]["parser"] == "docling"
