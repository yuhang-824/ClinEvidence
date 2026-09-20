"""OCR 方法选择、运行时配置和健康检测。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.config.options import (
    mineru_ocr_host_opts,
    mineru_official_api_opts,
    paddleocr_api_opts,
    pp_structure_v3_ocr_host_opts,
    system_options,
)
from yuxi.knowledge.parser.capabilities import OCR_FILE_EXTENSIONS, PARSER_CAPABILITIES, get_parser_capability
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.models.providers.service import get_model_provider_by_id, resolve_api_key


async def get_ocr_options(db: AsyncSession | None = None) -> dict[str, Any]:
    options = await system_options.get(db)
    return {
        "default_engine": options["default_ocr_engine"],
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


def resolve_ocr_engine_id(engine_id: str | None, default_engine: str) -> str:
    resolved = str(engine_id or default_engine).strip() or default_engine
    if resolved == "disable":
        return resolved
    if resolved not in PARSER_CAPABILITIES:
        raise ValueError(f"不支持的 OCR 引擎: {resolved}")
    return resolved


async def resolve_ocr_engine_id_for_params(
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> str:
    """只解析最终引擎标识，不构造引擎凭证。

    供按引擎分发而不使用其处理参数的调用方（如知识库 PDF 结构解析）使用，
    避免为无关引擎解析供应商密钥而扩大失败面。
    """
    configured_engine = (params or {}).get("ocr_engine")
    default_engine = (
        str(configured_engine)
        if configured_engine is not None
        else (await system_options.get(db))["default_ocr_engine"]
    )
    return resolve_ocr_engine_id(configured_engine, default_engine)


async def build_ocr_processor_kwargs(
    engine_id: str,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    """构造指定引擎的处理参数，供分发与任务参数解析共用。"""
    if engine_id == "disable":
        return {}
    if db is not None:
        return await _build_processor_kwargs(db, engine_id)
    from yuxi.storage.postgres.manager import pg_manager

    async with pg_manager.get_async_session_context() as session:
        return await _build_processor_kwargs(session, engine_id)


async def resolve_ocr_task_params(
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    resolved = dict(params or {})
    engine_id = await resolve_ocr_engine_id_for_params(resolved, db)
    resolved["ocr_engine"] = engine_id
    resolved.pop("ocr_engine_config", None)
    resolved["_ocr_processor_kwargs"] = await build_ocr_processor_kwargs(engine_id, db)
    return resolved


async def parse_document(
    source: str,
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> str:
    """使用当前运行时配置将文件解析为 Markdown。

    这是通用附件解析入口；知识库使用 parse_knowledge_document 保留 PDF 结构。函数负责区分应用层配置解析和
    底层文件转换：对于 PDF 与图片等 OCR 文件，先确定最终 OCR 引擎，再从
    数据库 Options、环境变量或模型供应商中解析该引擎的构造参数；对于普通
    文本、Office、表格等文件，参数保持原样并直接交给统一解析器。

    底层 parser 只接收已经准备好的 ``ocr_engine`` 和
    ``_ocr_processor_kwargs``，不查询数据库，也不关心配置值来自何处。调用方
    不应直接调用 ``yuxi.knowledge.parser.unified`` 中的内部解析入口，否则会
    绕过数据库配置、环境变量回退和默认 OCR 引擎解析。

    Args:
        source: 本地文件路径或系统支持的 MinIO 文件地址。
        params: 文件解析参数。可以包含 ``ocr_engine``、图片存储位置和各解析器
            支持的业务参数；未指定 OCR 引擎时使用系统默认值。
        db: 可选的异步数据库会话。已有事务的调用方可以传入以复用会话；未传入
            时仅在 OCR 配置解析需要查询数据库时创建独立会话。

    Returns:
        解析后的 Markdown 文本。

    Raises:
        ValueError: OCR 引擎无效、图片禁用 OCR 或文件类型不受支持。
        DocumentProcessorException: OCR 或文档解析器执行失败。
        StorageError: MinIO 文件读取失败。
    """

    resolved_params = params
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    if suffix in OCR_FILE_EXTENSIONS:
        resolved_params = await resolve_ocr_task_params(params, db)
        engine_id = resolved_params["ocr_engine"]
        if engine_id != "disable" and suffix not in get_parser_capability(engine_id).supported_extensions:
            raise ValueError(f"OCR 引擎 {engine_id} 不支持文件类型 {suffix}")

    from yuxi.knowledge.parser.unified import parse_resolved_document

    return await parse_resolved_document(source=source, params=resolved_params)


async def parse_knowledge_document(source, params, *, kb_id, file_id):
    """知识库 PDF 保存不可变结构原件，其他文档沿用清洗链路。

    PDF 解析引擎跟随 OCR 引擎选择：MinerU Official 走 MinerU 结构化解析，
    其余引擎保持本地 Docling（真实病历等敏感资料选本地引擎即可留在内网）。
    """
    import json
    import tempfile
    import uuid

    from yuxi.knowledge.cleaning import clean_document
    from yuxi.knowledge.structure import structure_report
    from yuxi.knowledge.utils.kb_utils import parse_minio_url
    from yuxi.storage.minio import get_minio_client

    if Path(source.split("?", 1)[0]).suffix.lower() != ".pdf":
        raw = await parse_document(source=source, params=params)
        content, report = clean_document(raw)
        return raw, content, report

    # 仅 PDF 需要在此提前确定引擎：非 PDF 由 parse_document 自行解析配置。
    # 这里只取引擎标识，不构造该引擎的凭证，避免无关引擎的配置缺失挡住本地解析。
    engine_id = await resolve_ocr_engine_id_for_params(params)
    client = get_minio_client()
    bucket, key = parse_minio_url(source)
    data = await client.adownload_file(bucket, key)
    with tempfile.TemporaryDirectory(prefix="knowledge-pdf-") as folder:
        path = Path(folder) / "source.pdf"
        await asyncio.to_thread(path.write_bytes, data)
        if engine_id == "mineru_official":
            import hashlib

            from yuxi.knowledge.parser.docling_pdf import check_native_coverage, page_has_visual_content
            from yuxi.knowledge.parser.mineru_official import MinerUOfficialParser
            from yuxi.knowledge.parser.mineru_structure import build_structure_from_mineru
            from yuxi.knowledge.structure import mark_auto_review

            parser_kwargs = await build_ocr_processor_kwargs(engine_id)
            parser = MinerUOfficialParser(api_key=parser_kwargs.get("api_key"), api_base=parser_kwargs.get("api_base"))
            parse_params = {
                **(params or {}),
                "image_bucket": client.KB_BUCKETS["images"],
                "image_prefix": f"{kb_id}/kb-images",
            }
            artifact = await asyncio.to_thread(parser.parse_structured_pdf, str(path), parse_params)
            raw = artifact["markdown"]
            structure = build_structure_from_mineru(
                artifact["layout"],
                artifact["content_list"],
                source_sha256=hashlib.sha256(data).hexdigest(),
            )
            if not structure["pages"]:
                raise ValueError("MinerU 未返回页面结构，无法进行结构审核，请稍后重试或改用本地引擎")
            structure["parser_version"] = artifact.get("model_version", "unknown")
            if artifact.get("origin_pdf"):

                def _coverage() -> None:
                    import pymupdf

                    with pymupdf.open(stream=artifact["origin_pdf"], filetype="pdf") as pdf:
                        pages = [p.get_text() for p in pdf]
                        visuals = [page_has_visual_content(p) for p in pdf]
                    if len(pages) != len(structure["pages"]):
                        structure["pages"][0]["issues"].append(
                            f"全文：原页文本层 {len(pages)} 页与解析 {len(structure['pages'])} 页不一致，"
                            "独立文本层比对已跳过，请逐页核对"
                        )
                        return
                    check_native_coverage(structure, pages, visual_pages=visuals)

                await asyncio.to_thread(_coverage)
            mark_auto_review(structure)
            document = {
                "parser": "mineru",
                "model_version": artifact.get("model_version", "unknown"),
                "content_list": artifact["content_list"],
                "layout": artifact["layout"],
            }
        else:
            from yuxi.knowledge.parser.docling_pdf import parse_structured_pdf

            document, raw, structure = await asyncio.to_thread(parse_structured_pdf, path)
    if len(raw) > 2_000_000 or len(structure["blocks"]) > 20000:
        raise ValueError("文档超过结构审核上限，请先拆分 PDF")
    object_name = f"{kb_id}/structure/{file_id}/{uuid.uuid4().hex}.json"
    uploaded = await client.aupload_file(
        bucket_name=client.KB_BUCKETS["parsed"],
        object_name=object_name,
        data=json.dumps(document, ensure_ascii=False).encode(),
        content_type="application/json",
    )
    structure["original_json"] = uploaded.url
    content, report = structure_report(structure)
    return raw, content, report


async def check_all_ocr_health(db: AsyncSession) -> dict[str, Any]:
    """使用当前有效配置并行检查所有 OCR 方法。"""

    configured = []
    results = {}
    for engine_id in PARSER_CAPABILITIES:
        try:
            kwargs = await _build_processor_kwargs(db, engine_id)
            configured.append((engine_id, kwargs))
        except Exception as exc:
            results[engine_id] = {"status": "error", "message": str(exc), "details": {}}

    async def check(engine_id: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await asyncio.to_thread(DocumentProcessorFactory.check_health, engine_id, **kwargs)
        except Exception as exc:
            result = {"status": "error", "message": str(exc), "details": {}}
        return engine_id, result

    checked = await asyncio.gather(*(check(engine_id, kwargs) for engine_id, kwargs in configured))
    results.update(checked)
    return results


async def _build_processor_kwargs(db: AsyncSession, engine_id: str) -> dict[str, Any]:
    if engine_id == "mineru_ocr":
        opts = await mineru_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "mineru_official":
        opts = await mineru_official_api_opts.get(db)
        return {"api_key": opts["api_key"]} if opts["api_key"] else {}
    if engine_id == "pp_structure_v3_ocr":
        opts = await pp_structure_v3_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "deepseek_ocr":
        provider = await get_model_provider_by_id(db, "siliconflow-cn")
        api_key = resolve_api_key(provider) if provider and provider.is_enabled else None
        if not api_key:
            raise ValueError("siliconflow-cn 模型供应商凭证不可用")
        return {
            "api_key": api_key,
            "api_url": f"{provider.base_url.rstrip('/')}/chat/completions",
        }
    if engine_id in {"paddleocr_vl_1_6", "paddleocr_pp_ocrv6"}:
        opts = await paddleocr_api_opts.get(db)
        return {key: value for key, value in opts.items() if value}
    return {}
