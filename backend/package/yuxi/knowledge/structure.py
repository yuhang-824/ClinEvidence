"""结构化审核稿拥有文块、来源映射与逐页核验契约。"""

import copy
import re
import unicodedata

from yuxi.utils.datetime_utils import utc_now

CHECKS = ("reading_order", "text_complete", "relationships")
KINDS = {"heading", "paragraph", "table", "relationship"}

# 机器核验：解析器证据充分、无需人工决策的页由系统放行，人工只处理被标记的页。
# 核验记录与人工签核分开存放，审计不把机器判断当成人工确认。
AUTO_REVIEW_RULE_BLANK = "blank-page/v1"
AUTO_REVIEW_RULE_CLEAN = "no-anomaly/v1"
# 这些 issue 来自解析器对空区域或缺失文本层的判断，不表示需要人工决策；其余 issue
# （结构可疑、表格未提取、图示等）都强制人工核验。按整串相等比较，未来新增的 issue
# 文案默认落在"需要人工"一侧，不会被前缀误判成可放行。
NON_BLOCKING_ISSUES = (
    "无可用 PDF 文本层，OCR 完整性须逐字对照原页",
    "页面没有正文文块：确认空白页或补录遗漏内容",
)
# 独立文本层比对允许的缺失比例：字体编码差异会造成个别字符对不上，未覆盖字符占比
# 超过该比例才判定为可能的内容丢失。
AUTO_REVIEW_MIN_COVERAGE = 0.99
ABSENT_CHARACTER_SAMPLE_LIMIT = 40
# 文本层比对的最小片段长度：标点与单字符片段不参与判定，避免把归一化差异当成整行丢失
MIN_MISSING_PIECE_CHARS = 2
# 片段切分：非字母数字字符（标点、空白）把文本层切成可比对的连续片段
MISSING_PIECE_SEPARATORS = re.compile(r"[^\w]+")
# 表格文块的正文是 HTML，标签字母会插在单元格文字之间，比对前先剥掉标签
HTML_TAG = re.compile(r"<[^>]*>")
# 解析器取不到文字时写入的占位正文，不能当作页面已有内容。空结构块的占位由解析器
# 直接排除（该区域本来就没有文字），表格与图示的占位保留给人工补充。
PLACEHOLDER_EMPTY_BLOCK = "[空结构块：请对照原页补充内容或注明排除原因]"
PLACEHOLDER_TABLE_BLOCK = "[表格：请对照原页核对行列与数值，或在文块修订中粘贴表格内容]"
PLACEHOLDER_FIGURE_BLOCK = "[图示：请对照原页补充文字与对应关系，或说明排除原因]"
PLACEHOLDER_PREFIXES = ("[空结构块", "[表格", "[图示")
EMPTY_BLOCK_EXCLUDED_NOTE = "解析器未取到该区域文字，已排除；原页如有内容请补充后取消排除"


def compose_structure(structure, *, allow_incomplete=False):
    """按审核顺序生成正文与来源位置；草稿可暂留历史空块与占位块。"""
    parts, spans, pages, offset = [], [], {}, 0
    for block in structure["blocks"]:
        if block.get("excluded"):
            continue
        text = block["text"].strip()
        if allow_incomplete and (not text or is_placeholder_text(text)):
            continue
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


def structure_report(structure, prior=None, *, allow_incomplete=False):
    """使审核稿和来源映射来自同一份结构数据。"""
    content, spans, pages = compose_structure(structure, allow_incomplete=allow_incomplete)
    report = {**(prior or {}), "structure": structure, "block_spans": spans, "page_spans": pages}
    report.pop("chunk_boundaries", None)
    report.setdefault("changes", [])
    report.setdefault("warnings", [])
    return content, report


def comparable_text(text):
    """归一为可比对的连续文本：去掉表格 HTML 标签、NFKC 兼容归一、大小写归一，只保留字母数字。"""
    return "".join(c for c in unicodedata.normalize("NFKC", HTML_TAG.sub("", text)).casefold() if c.isalnum())


def compared_characters(text):
    """提取参与文本层比对的字符集合。"""
    return set(comparable_text(text))


def missing_text_pieces(native_text, haystack):
    """找出文本层里在解析结果中找不到的连续片段。

    解析器会把跨页续排的段落整体归到起始页，因此判定必须在整份解析结果里查找，
    不能只看本页文块；按标点切成片段再比对，个别字符的归一化差异只会影响它所在的
    片段，不会让整行都被判为丢失。
    """
    missing = []
    for line in native_text.splitlines():
        for piece in MISSING_PIECE_SEPARATORS.split(line):
            needle = comparable_text(piece)
            if len(needle) >= MIN_MISSING_PIECE_CHARS and needle not in haystack:
                missing.append(piece.strip())
    return missing


def mark_auto_review(structure):
    """为无需人工决策的页记录机器核验，其余页保持人工核验。

    空白页要求页上没有任何可视内容（无文本层、无图像与图形、无正文文块），且解析器
    没有报告结构异常；判定为空白时同时移除该页上由版面模型臆造的占位文块与随之产生的
    提示，页面只保留空白页结论和补录入口。只在"没有文本层且没有正文"上判空白会把
    OCR 失败的扫描页当成空白页静默放行。其余页要求存在独立文本层比对记录、覆盖达标，
    且没有需要人工决策的异常。
    """
    for page in structure["pages"]:
        # 规则可重入：先撤销旧结论，避免对编辑过的结构重跑时留下过期的机器核验
        page.pop("auto_review", None)
        ordinary = [b for b in structure["blocks"] if b["page"] == page["page"] and not b.get("excluded")]
        if any(b["kind"] in {"table", "relationship"} for b in ordinary):
            continue
        check = page.get("native_text_check") or {}
        blocking = _has_blocking_issue(page)
        # 空白页必须有"无图像与图形"的实测证据；缺证据时不判空白
        if (
            check.get("characters") == 0
            and check.get("has_visual_content") is False
            and not _has_body_text(ordinary)
            and not blocking
        ):
            # 该页经证明没有内容：解析器仍可能给出覆盖整页的空结构块，会让人误以为内容被丢弃。
            # 只清掉占位文块，人工补录或排除的文块原样保留。
            structure["blocks"] = [
                b for b in structure["blocks"] if b["page"] != page["page"] or not is_placeholder_text(b["text"])
            ]
            page["issues"] = []
            page["auto_review"] = _auto_review_record(structure, AUTO_REVIEW_RULE_BLANK)
            continue
        if not check or coverage_shortfall(check) or blocking:
            continue
        page["auto_review"] = _auto_review_record(structure, AUTO_REVIEW_RULE_CLEAN)


def coverage_shortfall(check):
    """判断独立文本层比对是否显示内容未覆盖；生产者与规则共用同一判据。"""
    coverage = check.get("coverage")
    return coverage is None or coverage < AUTO_REVIEW_MIN_COVERAGE


def _has_blocking_issue(page):
    """判断该页是否存在需要人工决策的解析异常。"""
    return any(issue not in NON_BLOCKING_ISSUES for issue in page.get("issues", []))


def is_placeholder_text(text):
    """判断文块正文是否仍是解析器写入的占位文本。"""
    return bool(text) and text.lstrip().startswith(PLACEHOLDER_PREFIXES)


def _has_body_text(blocks):
    """判断页内是否存在解析器未以占位文本代替的正文。"""
    return any(b["text"].strip() and not is_placeholder_text(b["text"]) for b in blocks)


def _auto_review_record(structure, rule):
    """记录机器核验结论与产生结论的解析器身份。"""
    return {
        "rule": rule,
        "parser": structure.get("parser"),
        "parser_version": structure.get("parser_version"),
        "at": utc_now().isoformat(),
    }


def revise_structure(original, payload, operator, timestamp):
    """只接受编辑字段，源文本、坐标和原始文块身份不可由客户端覆盖。"""
    if not isinstance(payload, dict) or set(payload) != {"blocks", "pages"}:
        raise ValueError("结构修订须包含文块和逐页核验")
    blocks, pages = payload["blocks"], payload["pages"]
    if not isinstance(blocks, list) or len(blocks) > 20000 or not isinstance(pages, list):
        raise ValueError("结构修订大小无效")
    source = {b["id"]: b for b in original["blocks"]}
    page_source = {p["page"]: p for p in original["pages"]}
    updated, seen, last_page, touched = [], set(), 0, set()
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
        # 与解析器产出对比时使用同一套默认值，缺字段不等于被人改过
        unchanged = (
            block.get("text"),
            block.get("note", ""),
            block.get("kind"),
            block.get("level", 2),
            bool(block.get("excluded", False)),
        ) == (
            text,
            note,
            kind,
            level,
            excluded,
        )
        # 旧稿的空块或占位块可暂留；新建或修改该块时必须补正文或明确排除
        if not excluded and not text.strip() and not unchanged:
            raise ValueError("非排除文块不能为空，请补充原文或注明排除原因")
        if not excluded and is_placeholder_text(text) and not unchanged:
            raise ValueError("占位文块没有正文，请补充原文或勾选不参与检索")
        if block_id not in source or not unchanged:
            touched.add(page)
        block.update(text=text, note=note, kind=kind, level=level, excluded=excluded)
        updated.append(block)
        seen.add(block_id)
    if not set(source) <= seen:
        raise ValueError("不能删除原始文块；请保留并注明排除原因")
    if sum(len(b["text"]) for b in updated) > 2_000_000:
        raise ValueError("审核稿超过长度上限")
    if len(pages) != len(page_source):
        raise ValueError("必须保留全部页面的核验记录")
    # 页内阅读顺序也是人工判断，顺序变化同样使机器核验失效
    revised_order, stored_order = _block_ids_by_page(updated), _block_ids_by_page(original["blocks"])
    touched.update(page for page, ids in revised_order.items() if ids != stored_order.get(page))
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
        entry = {
            **page_source[number],
            "checks": checks,
            "note": note,
            "reviewed_by": operator if all(checks.values()) else None,
            "reviewed_at": timestamp if all(checks.values()) else None,
        }
        if number in touched or note != page_source[number].get("note", ""):
            # 正文或页面说明被人工修改后，机器核验不再覆盖当前内容，该页需要重新核验
            entry.pop("auto_review", None)
        page_updates.append(entry)
        checked.add(number)
    return {**original, "blocks": updated, "pages": sorted(page_updates, key=lambda p: p["page"])}


def _block_ids_by_page(blocks):
    """按出现顺序归集每页的文块标识，用于判断页内阅读顺序是否变化。"""
    order = {}
    for block in blocks:
        order.setdefault(block["page"], []).append(block["id"])
    return order


def require_structure_review(report):
    """审批与索引共同执行逐页核验，不将转换成功当作语义正确。

    机器核验页由解析器证据放行，不要求人工签核；文件级审批仍是人工动作，
    因此页级证据分机器与人工，文档仍有人负责。
    """
    structure = report.get("structure")
    if not structure:
        return
    placeholders = [b for b in structure["blocks"] if not b.get("excluded") and is_placeholder_text(b["text"])]
    if placeholders:
        # 占位正文不是内容：它可能是解析器没取到文字，也可能是表格/图示待人工补充。
        # 审批与索引都拦在这里，避免绕过前端校验的旧版本把占位句写进检索结果。
        pages = "、".join(str(page) for page in sorted({b["page"] for b in placeholders}))
        raise ValueError(f"第 {pages} 页存在未处理的占位文块，请补充原文或勾选不参与检索")
    for page in structure["pages"]:
        if page.get("auto_review"):
            continue
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
