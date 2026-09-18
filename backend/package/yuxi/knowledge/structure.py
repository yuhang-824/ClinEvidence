"""结构化审核稿拥有文块、来源映射与逐页核验契约。"""

import copy
import re

CHECKS = ("reading_order", "text_complete", "relationships")
KINDS = {"heading", "paragraph", "table", "relationship"}


def compose_structure(structure):
    """按审核顺序生成唯一正文，同时保留块和页的字符位置。"""
    parts, spans, pages, offset = [], [], {}, 0
    for block in structure["blocks"]:
        if block.get("excluded"):
            continue
        text = block["text"].strip()
        if not text:
            raise ValueError("非排除文块不能为空，请补充原文或注明排除原因")
        if block["kind"] == "heading":
            text = "#" * block.get("level", 2) + " " + text.lstrip("# ")
        if parts:
            offset += 2
        start = offset
        parts.append(text)
        offset += len(text)
        spans.append(
            {
                "id": block["id"],
                "page": block["page"],
                "bbox": block.get("bbox"),
                "kind": block["kind"],
                "start": start,
                "end": offset,
            }
        )
        page = pages.setdefault(block["page"], {"page": block["page"], "start": start, "end": offset})
        page["end"] = offset
    return "\n\n".join(parts), spans, list(pages.values())


def structure_report(structure, prior=None):
    """使审核稿和来源映射来自同一份结构数据。"""
    content, spans, pages = compose_structure(structure)
    report = {**(prior or {}), "structure": structure, "block_spans": spans, "page_spans": pages}
    report.pop("chunk_boundaries", None)
    report.setdefault("changes", [])
    report.setdefault("warnings", [])
    return content, report


def revise_structure(original, payload, operator, timestamp):
    """只接受编辑字段，源文本、坐标和原始文块身份不可由客户端覆盖。"""
    if not isinstance(payload, dict) or set(payload) != {"blocks", "pages"}:
        raise ValueError("结构修订须包含文块和逐页核验")
    blocks, pages = payload["blocks"], payload["pages"]
    if not isinstance(blocks, list) or len(blocks) > 20000 or not isinstance(pages, list):
        raise ValueError("结构修订大小无效")
    source = {b["id"]: b for b in original["blocks"]}
    page_source = {p["page"]: p for p in original["pages"]}
    updated, seen, last_page = [], set(), 0
    for change in blocks:
        if not isinstance(change, dict) or set(change) - {"id", "page", "text", "kind", "level", "excluded", "note"}:
            raise ValueError("包含不支持的文块字段")
        block_id, page = change.get("id"), change.get("page")
        if not isinstance(block_id, str) or block_id in seen or type(page) is not int or page not in page_source:
            raise ValueError("文块标识或页码无效")
        if page < last_page:
            raise ValueError("请在原页内调整阅读顺序，不能改变来源页")
        last_page = page
        if block_id in source:
            block = copy.deepcopy(source[block_id])
            if block["page"] != page:
                raise ValueError("不能改变文块来源页")
        elif re.fullmatch(r"new:[a-zA-Z0-9-]{1,64}", block_id):
            block = {"id": block_id, "page": page, "bbox": None, "source_text": "", "manual": True}
        else:
            raise ValueError("未知文块标识")
        text, note, kind = change.get("text"), change.get("note", ""), change.get("kind")
        excluded = change.get("excluded", False)
        if not isinstance(text, str) or len(text) > 200000 or not isinstance(note, str) or len(note) > 4000:
            raise ValueError("文块正文或核验说明无效")
        if kind not in KINDS or type(excluded) is not bool:
            raise ValueError("文块类型无效")
        level = change.get("level", block.get("level", 2))
        if type(level) is not int or not 1 <= level <= 6:
            raise ValueError("标题层级必须为 1 到 6")
        if excluded and not note.strip():
            raise ValueError("排除文块必须说明原因，原文仍会保留")
        block.update(text=text, note=note, kind=kind, level=level, excluded=excluded)
        updated.append(block)
        seen.add(block_id)
    if not set(source) <= seen:
        raise ValueError("不能删除原始文块；请保留并注明排除原因")
    if sum(len(b["text"]) for b in updated) > 2_000_000:
        raise ValueError("审核稿超过长度上限")
    if len(pages) != len(page_source):
        raise ValueError("必须保留全部页面的核验记录")
    page_updates, checked = [], set()
    for change in pages:
        if not isinstance(change, dict) or set(change) != {"page", "checks", "note"}:
            raise ValueError("逐页核验字段无效")
        number, checks, note = change["page"], change["checks"], change["note"]
        if type(number) is not int or number not in page_source or number in checked:
            raise ValueError("核验页码重复或无效")
        if (
            not isinstance(checks, dict)
            or set(checks) != set(CHECKS)
            or any(type(v) is not bool for v in checks.values())
        ):
            raise ValueError("必须分别核验阅读顺序、完整性和对应关系")
        if not isinstance(note, str) or len(note) > 4000:
            raise ValueError("页面核验说明无效")
        page_updates.append(
            {
                **page_source[number],
                "checks": checks,
                "note": note,
                "reviewed_by": operator if all(checks.values()) else None,
                "reviewed_at": timestamp if all(checks.values()) else None,
            }
        )
        checked.add(number)
    return {**original, "blocks": updated, "pages": sorted(page_updates, key=lambda p: p["page"])}


def require_structure_review(report):
    """审批与索引共同执行逐页核验，不将转换成功当作语义正确。"""
    structure = report.get("structure")
    if not structure:
        return
    for page in structure["pages"]:
        if not all(page.get("checks", {}).get(k) is True for k in CHECKS) or not page.get("reviewed_by"):
            raise ValueError(f"第 {page['page']} 页尚未完成阅读顺序、完整性和对应关系核验")
        special = any(
            b["page"] == page["page"] and (b["kind"] in {"table", "relationship"} or b.get("excluded"))
            for b in structure["blocks"]
        )
        if (special or page.get("issues")) and not page.get("note", "").strip():
            raise ValueError(f"第 {page['page']} 页需要填写表格、图示或异常核验说明")
    content, spans, pages = compose_structure(structure)
    if spans != report.get("block_spans") or pages != report.get("page_spans"):
        raise ValueError("结构来源映射失效，请重新保存结构稿")
    return content
