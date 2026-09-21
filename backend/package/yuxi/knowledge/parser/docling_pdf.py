"""离线 Docling PDF 解析及可审核的结构投影。"""

import copy
import hashlib
import os
import re
import threading
from functools import lru_cache
from pathlib import Path

from yuxi.knowledge.structure import (
    ABSENT_CHARACTER_SAMPLE_LIMIT,
    CHECKS,
    EMPTY_BLOCK_EXCLUDED_NOTE,
    PLACEHOLDER_EMPTY_BLOCK,
    PLACEHOLDER_FIGURE_BLOCK,
    comparable_text,
    compared_characters,
    coverage_shortfall,
    mark_auto_review,
    missing_text_pieces,
)

_parse_lock = threading.Lock()


def split_text_sources(original):
    """按字符来源展开跨页文字；无法确定归属时显式失败，禁止伪造第一页来源。"""
    document = copy.deepcopy(original)
    expanded, refs = [], {}
    for item in document.get("texts", []):
        prov = item.get("prov", [])
        if len(prov) <= 1:
            expanded.append(item)
            continue
        text = item["text"]
        if len({p["page_no"] for p in prov}) == 1:
            number = prov[0]["page_no"]
            height = document["pages"][str(number)]["size"]["height"]
            boxes = []
            for source in prov:
                box = source["bbox"]
                top, bottom = (
                    (height - box["t"], height - box["b"])
                    if box.get("coord_origin") == "BOTTOMLEFT"
                    else (box["t"], box["b"])
                )
                boxes.append((box["l"], top, box["r"], bottom))
            bbox = {
                "l": min(b[0] for b in boxes),
                "t": min(b[1] for b in boxes),
                "r": max(b[2] for b in boxes),
                "b": max(b[3] for b in boxes),
                "coord_origin": "TOPLEFT",
            }
            expanded.append({**item, "source_provenance": prov, "prov": [{"page_no": number, "bbox": bbox}]})
            continue
        covered, parts = set(), []
        for i, source in enumerate(prov):
            start, end = source.get("charspan", [-1, -1])
            positions = set(range(start, end))
            if not 0 <= start < end <= len(text) or positions & covered:
                raise ValueError(f"文块 {item['self_ref']} 的跨页字符范围无效或重叠，请核对原 PDF 后重新解析")
            covered.update(positions)
            part = {
                **item,
                "self_ref": f"{item['self_ref']}:part:{i + 1}",
                "source_ref": item["self_ref"],
                "text": text[start:end],
                "prov": [source],
                "children": item.get("children", []) if i == 0 else [],
            }
            if i and part.get("label") in {"title", "section_header"}:
                part["label"] = "text"
            parts.append(part)
        if any(i not in covered and not c.isspace() for i, c in enumerate(text)):
            raise ValueError(f"文块 {item['self_ref']} 有无法定位到原页的文字，请核对原 PDF 后重新解析")
        refs[item["self_ref"]] = [p["self_ref"] for p in parts]
        expanded.extend(parts)
    document["texts"] = expanded
    for name in ("tables", "pictures"):
        if any(len(item.get("prov", [])) > 1 for item in document.get(name, [])):
            raise ValueError("存在无法按字符定位的多来源表格或图示，请先按页拆分该 PDF 后逐页审核")
    nodes = [document.get("body", {}), document.get("furniture", {})]
    nodes += [item for name in ("texts", "tables", "pictures", "groups") for item in document.get(name, [])]
    for node in nodes:
        node["children"] = [
            {"$ref": ref} for child in node.get("children", []) for ref in refs.get(child["$ref"], [child["$ref"]])
        ]
    return document


def build_structure(document, tables):
    """保留原页文块，图片子文本与未进入阅读树的文本均不得静默丢弃。"""
    document = split_text_sources(document)
    pages = [
        {
            "page": int(n),
            "width": p["size"]["width"],
            "height": p["size"]["height"],
            "checks": dict.fromkeys(CHECKS, False),
            "note": "",
            "issues": [],
        }
        for n, p in document["pages"].items()
    ]
    by_page = {p["page"]: p for p in pages}
    items = {x["self_ref"]: x for name in ("texts", "tables", "pictures", "groups") for x in document.get(name, [])}
    blocks, seen = [], set()

    def descendants(ref):
        """回读图片内的全部文字，保留块间隔而不猜测箭头语义。"""
        if ref in seen:
            return []
        seen.add(ref)
        item = items[ref]
        value = tables.get(ref, item.get("text", ""))
        texts = [value] if value else []
        for child in item.get("children", []):
            texts.extend(descendants(child["$ref"]))
        return texts

    def visit(ref, orphan=False):
        """按文档阅读树生成具备不可变来源的文块。"""
        if ref in seen:
            return
        item = items[ref]
        label = item.get("label", "")
        if not item.get("prov"):
            seen.add(ref)
            for child in item.get("children", []):
                visit(child["$ref"], orphan)
            return
        provenance = item["prov"][0]
        number = provenance["page_no"]
        page = by_page[number]
        if item.get("source_provenance"):
            page["issues"].append("同一文块含多个原页区域，已完整保留文字并显示联合范围，请核对块内阅读顺序")
        bbox = provenance["bbox"]
        if bbox.get("coord_origin") == "BOTTOMLEFT":
            box = [bbox["l"], page["height"] - bbox["t"], bbox["r"], page["height"] - bbox["b"]]
        else:
            box = [bbox["l"], bbox["t"], bbox["r"], bbox["b"]]
        if label == "picture":
            texts = descendants(ref)
            text = "\n\n".join(texts) or PLACEHOLDER_FIGURE_BLOCK
            kind = "relationship"
            page["issues"].append("图示需人工核对内部文字、条件与分支关系")
        else:
            seen.add(ref)
            text = tables.get(ref, item.get("text", ""))
            kind = "table" if label == "table" else "heading" if label in {"title", "section_header"} else "paragraph"
        excluded = label in {"page_header", "page_footer"}
        note = "解析器标记为页眉/页脚，请人工确认排除" if excluded else ""
        if not text.strip():
            # 解析器没取到文字的区域没有可索引内容：直接排除，避免占位文本进正文。
            # 情况记在文块说明里，不再重复产生页面级 issue（那会让机器核验页看起来自相矛盾）
            text, excluded, note = PLACEHOLDER_EMPTY_BLOCK, True, EMPTY_BLOCK_EXCLUDED_NOTE
        level = min(6, int(item.get("level", 2))) if kind == "heading" else 2
        if kind == "heading":
            match = re.match(r"^(\d+(?:\.\d+)*)\s+\S", text)
            if match:
                level = min(6, match[1].count(".") + 1)
            if re.fullmatch(r"[\d.]+", text.strip()) or re.search(r"[。；，]$", text.strip()):
                page["issues"].append("标题疑似正文续句或编号与标题分离，请修订文块类型和内容")
        blocks.append(
            {
                "id": ref,
                "source_ref": item.get("source_ref", ref),
                "source_provenance": item.get("source_provenance", item["prov"]),
                "page": number,
                "bbox": box,
                "source_label": label,
                "kind": kind,
                "level": max(1, level),
                "source_text": text,
                "text": text,
                "excluded": excluded,
                "note": note,
            }
        )
        if orphan:
            page["issues"].append("存在未进入主阅读树的文字，已保留，请核对位置")
        if len(item["prov"]) > 1:
            page["issues"].append("文块有多处来源，请核对跨页关系")
        for child in item.get("children", []):
            visit(child["$ref"], orphan)

    for root in ("body", "furniture"):
        for child in document.get(root, {}).get("children", []):
            visit(child["$ref"])
    for ref, item in items.items():
        if item.get("prov") and ref not in seen:
            visit(ref, True)
    blocks.sort(key=lambda b: b["page"])
    for page in pages:
        if not any(b["page"] == page["page"] and not b["excluded"] for b in blocks):
            page["issues"].append("页面没有正文文块：确认空白页或补录遗漏内容")
        page["issues"] = list(dict.fromkeys(page["issues"]))
    return {"schema": 1, "parser": "docling", "pages": sorted(pages, key=lambda p: p["page"]), "blocks": blocks}


def page_has_visual_content(page):
    """判断原页是否存在图像或矢量图形，用于区分空白页与解析器漏读的扫描页。

    嵌套在 Form XObject 里的图像不出现在 `get_images()` 中，用文本页的图片块补充判断；
    宁可判为"有可视内容"要求人工核对，也不要把有内容的页当作空白页放行。
    """
    if page.get_images() or page.get_drawings():
        return True
    return any(block.get("type") == 1 for block in page.get_text("dict")["blocks"])


def check_native_coverage(structure, native_pages, *, visual_pages=None):
    """用独立文本层发现疑似遗漏；字符覆盖不证明阅读顺序或语义正确。

    比对按"本页文本层里有没有解析结果完全找不到的连续片段"进行，查找范围是整份解析
    结果：解析器会把跨页续排的段落整体归到起始页，只看本页文块会把续排内容误报成
    缺失。解析器会归一化的码位（圈号、罗马数字、全角字母数字）两侧都做 NFKC 归一，
    重复出现的页眉表头也不再被当成缺失。visual_pages 是与 native_pages 对齐的可视内容
    证据；缺失时不写入，页据此不能判为空白。
    """
    haystack = "\x00".join(comparable_text(block["source_text"]) for block in structure["blocks"])
    for index, (page, native) in enumerate(zip(structure["pages"], native_pages, strict=True)):
        expected = compared_characters(native)
        missing = missing_text_pieces(native, haystack)
        absent = {character for piece in missing for character in compared_characters(piece)}
        sample = "…".join(missing)[:ABSENT_CHARACTER_SAMPLE_LIMIT]
        evidence = {
            "characters": len(expected),
            "absent_characters": len(absent),
            "absent_sample": sample,
            "coverage": round(1 - len(absent) / len(expected), 4) if expected else None,
        }
        if visual_pages is not None:
            evidence["has_visual_content"] = bool(visual_pages[index])
        page["native_text_check"] = evidence
        if not expected:
            page["issues"].append("无可用 PDF 文本层，OCR 完整性须逐字对照原页")
        elif coverage_shortfall(evidence):
            page["issues"].append(
                f"独立文本层比对：{len(absent)}/{len(expected)} 个字符未出现在解析结果中"
                f"（如「{sample}」），请对照原页核实"
            )


@lru_cache(maxsize=1)
def get_converter():
    """仅在实际解析时加载本地模型，限制 CPU 与并发占用。"""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    artifacts = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/home/yuxi/.cache/docling/models"))
    if not artifacts.is_dir():
        raise ValueError("Docling 离线模型未准备，请先安装版面、表格和 OCR 模型")
    options = PdfPipelineOptions(
        do_ocr=True, do_table_structure=True, enable_remote_services=False, artifacts_path=artifacts
    )
    # 中文识别器同时覆盖拉丁文字；RapidOCR 不支持一次指定两个识别器。
    options.ocr_options = RapidOcrOptions(backend="onnxruntime", lang=["ch"])
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    options.accelerator_options = AcceleratorOptions(num_threads=2, device=AcceleratorDevice.CPU)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def parse_structured_pdf(path):
    """完整保留 Docling JSON，转换失败不会降级成可入库纯文本。"""
    from importlib.metadata import version

    from docling.datamodel.base_models import ConversionStatus

    with _parse_lock:
        result = get_converter().convert(path)
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError(f"Docling 未完成 PDF 解析：{result.status}")
        document = result.document
        raw = document.export_to_markdown(traverse_pictures=True)
        data = document.export_to_dict()
        structure = build_structure(data, {t.self_ref: t.export_to_markdown(doc=document) for t in document.tables})
        import pymupdf

        with pymupdf.open(path) as pdf:
            check_native_coverage(
                structure,
                [p.get_text() for p in pdf],
                visual_pages=[page_has_visual_content(p) for p in pdf],
            )
        structure.update(
            parser_version=version("docling-slim"), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest()
        )
        mark_auto_review(structure)
        return data, raw, structure
