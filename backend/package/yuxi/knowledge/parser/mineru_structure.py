"""MinerU 版面输出到结构审核契约的适配。

坐标与块来源是 MinerU 的 layout.json（与原页坐标一致且保持阅读顺序）；
content_list 仅用于补充表格 HTML，两份列表按页内顺序对齐，对不齐时降级为
占位文本而不是猜测对应关系。规则与 docling_pdf.build_structure 保持同一
结构契约（schema 1），字段语义见 structure.py。
"""

import re

from yuxi.knowledge.structure import CHECKS

_TITLE_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\s*\S")

# layout 的 para_blocks 与 content_list 用不同词汇表；对齐前先归一，顺序不一致则整体降级
_TYPE_ALIASES = {"title": "text", "paragraph": "text"}
_CONTENT_TYPES = {"title", "text", "table", "image", "equation", "paragraph"}


def _span_text(block: dict) -> str:
    """聚合块的行内文字；表格等图像裁剪块天然为空。"""
    lines = block.get("lines") or []
    return "".join(span.get("content", "") for line in lines for span in (line.get("spans") or []))


def _nested_text(block: dict) -> str:
    """聚合嵌套块（表格标题、图注等）的文字。"""
    texts = []
    for nested in block.get("blocks") or []:
        if nested.get("type") in {"image_caption", "img_caption", "table_caption", "table_footnote"}:
            value = _span_text(nested)
            if value:
                texts.append(value)
    return "\n\n".join(texts)


def _heading_level(block: dict, text: str) -> int:
    """优先用 MinerU 的层级判断，再按编号深度修正，与 docling 适配保持一致。"""
    level = block.get("level")
    if isinstance(level, int) and 1 <= level <= 6:
        base = level
    else:
        base = 2
    match = _TITLE_NUMBER.match(text)
    if match:
        base = match[1].count(".") + 1
    return max(1, min(6, base))


def _normalized_type(value: object) -> str:
    """把两种输出的块类型归一到同一词汇，用于判断页内顺序是否一致。"""
    name = str(value or "").strip().lower()
    return _TYPE_ALIASES.get(name, name)


def _table_text(entry: dict | None) -> str:
    if not entry:
        return ""
    html = entry.get("table_body")
    if isinstance(html, str) and html.strip():
        return html.strip()
    return ""


def build_structure_from_mineru(
    layout: dict,
    content_list: list | None = None,
    *,
    source_sha256: str | None = None,
) -> dict:
    """把 MinerU layout/content_list 输出转换为结构审核契约。"""
    content_entries = [
        entry
        for entry in (content_list or [])
        if isinstance(entry, dict) and entry.get("type") not in {"header", "page_number"}
    ]

    pages, blocks = [], []
    for position, page_info in enumerate(layout.get("pdf_info", [])):
        page_index = page_info.get("page_idx")
        number = int(page_index if page_index is not None else position) + 1
        size = page_info.get("page_size") or [595, 842]
        current_page = {
            "page": number,
            "width": float(size[0]),
            "height": float(size[1]),
            "checks": dict.fromkeys(CHECKS, False),
            "note": "",
            "issues": [],
        }
        pages.append(current_page)

        para_blocks = page_info.get("para_blocks") or []
        page_entries = [entry for entry in content_entries if int(entry.get("page_idx", 0)) + 1 == number]
        content_types = [
            _normalized_type(entry.get("type")) for entry in page_entries if entry.get("type") in _CONTENT_TYPES
        ]
        block_types = [_normalized_type(block.get("type")) for block in para_blocks]
        aligned = (
            [entry for entry in page_entries if entry.get("type") in _CONTENT_TYPES]
            if content_types == block_types
            else []
        )

        for order, block in enumerate(para_blocks):
            block_type = str(block.get("type", "text"))
            text = _span_text(block)
            issues = []
            if block_type == "title":
                kind = "heading"
                level = _heading_level(block, text)
            elif block_type == "table":
                kind = "table"
                level = 2
            elif block_type == "image":
                kind = "relationship"
                level = 2
                issues.append("图示需人工核对内部文字、条件与分支关系")
            else:
                kind = "paragraph"
                level = 2

            if kind == "table":
                table_text = _table_text(aligned[order]) if aligned else ""
                if not table_text:
                    table_text = _nested_text(block)
                if not table_text:
                    table_text = "[表格：请对照原页核对行列与数值，或在文块修订中粘贴表格内容]"
                    issues.append("表格内容未能自动提取，请对照原页核对")
                text = table_text
            elif kind == "relationship":
                caption = _nested_text(block)
                text = caption or "[图示：请对照原页补充文字与对应关系，或说明排除原因]"
            elif kind == "heading":
                if re.fullmatch(r"[\d.]+", text.strip()) or re.search(r"[。；，]$", text.strip()):
                    issues.append("标题疑似正文续句或编号与标题分离，请修订文块类型和内容")

            if not text.strip():
                issues.append("存在空结构块，请对照原文检查")
                text = "[空结构块：请对照原页补充内容或注明排除原因]"

            blocks.append(
                {
                    "id": f"m{page_info.get('page_idx', 0)}_{block.get('index', order)}",
                    "page": number,
                    "bbox": [float(v) for v in block.get("bbox", [0, 0, 0, 0])],
                    "source_label": block_type,
                    "kind": kind,
                    "level": level,
                    "source_text": text,
                    "text": text,
                    "excluded": False,
                    "note": "",
                }
            )
            current_page["issues"].extend(issues)

        for discarded in page_info.get("discarded_blocks") or []:
            text = _span_text(discarded)
            if not text.strip():
                continue
            blocks.append(
                {
                    "id": f"d{page_info.get('page_idx', 0)}_{len(blocks)}",
                    "page": number,
                    "bbox": [float(v) for v in discarded.get("bbox", [0, 0, 0, 0])],
                    "source_label": str(discarded.get("type", "discarded")),
                    "kind": "paragraph",
                    "level": 2,
                    "source_text": text,
                    "text": text,
                    "excluded": True,
                    "note": "解析器判定为页眉/页脚或噪声，请人工确认排除",
                }
            )

    blocks.sort(key=lambda b: b["page"])
    for page in pages:
        if not any(b["page"] == page["page"] and not b["excluded"] for b in blocks):
            page["issues"].append("页面没有正文文块：确认空白页或补录遗漏内容")
        page["issues"] = list(dict.fromkeys(page["issues"]))

    structure = {
        "schema": 1,
        "parser": "mineru",
        "pages": sorted(pages, key=lambda p: p["page"]),
        "blocks": blocks,
    }
    if source_sha256:
        structure["source_sha256"] = source_sha256
    return structure
