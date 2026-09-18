"""本地 PDF 的坐标排序、规则表格与字段行组织。"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

import pdfplumber


@dataclass
class TextBox:
    """保留文本区域的页面坐标。"""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float


def _column_gutter(gaps, width, min_support):
    """以空隙端点分区寻找多行共有栏缝，避免短行中点偏移。"""
    events = {}
    for left, right in gaps:
        events[left] = events.get(left, 0) + 1
        events[right] = events.get(right, 0) - 1
    points = sorted(events)
    support = 0
    best = None
    for left, right in zip(points, points[1:]):
        support += events[left]
        center = (left + right) / 2
        if support >= min_support:
            candidate = (support, -abs(center - width / 2), center)
            if best is None or candidate > best:
                best = candidate
    return best[2] if best else None


def image_rules(image):
    """从扫描图检测水平和垂直表格线，坐标保持像素单位。"""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    results = []
    for horizontal in (True, False):
        size = (max(30, image.width // 20), 1) if horizontal else (1, max(30, image.height // 30))
        mask = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, size))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        edges = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if horizontal:
                edges.append((x, y + h / 2, x + w))
            else:
                edges.append((x + w / 2, y, y + h))
        results.append(edges)
    return results


def parse_local_pdf(path, ocr_page=None, page_separator="\n\n") -> str:
    """优先读取文字页；图片页由调用者提供的坐标 OCR 处理。"""
    pages = []
    with pdfplumber.open(path) as document:
        for page in document.pages:
            needs_ocr = bool(page.images) or any(not c.get("upright", True) for c in page.chars)
            needs_ocr = needs_ocr or any("(cid:" in c["text"] or "\ufffd" in c["text"] for c in page.chars)
            needs_ocr = needs_ocr or (not page.chars and bool(page.curves))
            if needs_ocr:
                if ocr_page is None:
                    raise ValueError(f"第 {page.page_number} 页需要图像/旋转文字识别，请启用 OCR 或版面解析引擎")
                boxes, horizontal, vertical = ocr_page(page.page_number - 1, page.width, page.height)
            else:
                boxes = native_text_boxes(page)
                horizontal = [(e["x0"], e["top"], e["x1"]) for e in page.edges if e["orientation"] == "h"]
                vertical = [(e["x0"], e["top"], e["bottom"]) for e in page.edges if e["orientation"] == "v"]
            text = render_layout(boxes, page.width, horizontal, vertical)
            fields = form_fields(page)
            if fields:
                text = "\n\n".join([text, *(field.text for field in fields)]).strip()
            pages.append(text)
    return page_separator.join(pages)


def native_text_boxes(page) -> list[TextBox]:
    """将文字层按几何位置聚为行，保留栏间空隙。"""
    chars = [c for c in page.dedupe_chars().chars if c.get("text")]
    if not chars:
        return []
    # 同一基线上的中文与拉丁字体可能报告不同的字体包围盒，使用文本矩阵定位。
    chars = [
        {**c, "bottom": page.height - c["matrix"][5], "top": page.height - c["matrix"][5] - c["size"]} for c in chars
    ]
    tolerance = max(2, median(c["height"] for c in chars) * 0.55)
    rows = []
    for char in sorted(chars, key=lambda c: ((c["top"] + c["bottom"]) / 2, c["x0"])):
        center = (char["top"] + char["bottom"]) / 2
        if not rows or center - rows[-1][0] > tolerance:
            rows.append((center, [char]))
        else:
            rows[-1][1].append(char)
    boxes = []
    # 栏边界由多行重复空隙确认；个别行末标点伸入栏间时不能合成跨栏块。
    gaps = []
    for _, row in rows:
        ordered = sorted(row, key=lambda c: c["x0"])
        for left, right in zip(ordered, ordered[1:]):
            center = (left["x1"] + right["x0"]) / 2
            if (
                right["x0"] - left["x1"] >= max(6, min(left["size"], right["size"]) * 0.75)
                and page.width * 0.3 < center < page.width * 0.7
            ):
                gaps.append((left["x1"], right["x0"]))
    gutter = _column_gutter(gaps, page.width, max(3, len(rows) * 0.4))
    vertical_edges = [e for e in page.edges if e["orientation"] == "v"]
    for _, row in rows:
        runs = []
        for char in sorted(row, key=lambda c: c["x0"]):
            crosses_cell = runs and any(
                runs[-1][-1]["x1"] - 1 <= edge["x0"] <= char["x0"] + 1
                and edge["top"] <= char["bottom"] <= edge["bottom"] + 2
                for edge in vertical_edges
            )
            crosses_gutter = (
                runs
                and gutter is not None
                and runs[-1][-1]["x1"] < gutter < char["x0"]
                and char["x0"] - runs[-1][-1]["x1"] >= max(6, char["size"] * 0.75)
            )
            if (
                not runs
                or crosses_cell
                or crosses_gutter
                or char["x0"] - runs[-1][-1]["x1"] > max(12, page.width * 0.018)
            ):
                runs.append([char])
            else:
                runs[-1].append(char)
        for run in runs:
            text = ""
            previous = None
            for char in run:
                if previous and char["x0"] - previous["x1"] > max(1, char["size"] * 0.2):
                    text += " "
                text += char["text"]
                previous = char
            if text.strip():
                boxes.append(
                    TextBox(
                        text.strip(),
                        min(c["x0"] for c in run),
                        min(c["top"] for c in run),
                        max(c["x1"] for c in run),
                        max(c["bottom"] for c in run),
                    )
                )
    return boxes


def form_fields(page) -> list[TextBox]:
    """读取交互式 PDF 字段值，避免遗漏未写入正文流的表单数据。"""
    from pdfminer.pdftypes import resolve1
    from pdfminer.psparser import PSLiteral
    from pdfminer.utils import decode_text

    def value_text(value):
        """解码 PDF 字段的原始值，不推断选项含义。"""
        value = resolve1(value)
        if isinstance(value, bytes):
            return decode_text(value)
        if isinstance(value, PSLiteral):
            return value.name
        if isinstance(value, list):
            return ", ".join(value_text(item) for item in value)
        return str(value) if value is not None else ""

    fields = []
    for annotation in page.annots or []:
        data = annotation["data"]
        if value_text(data.get("Subtype")) != "Widget":
            continue
        inherited, seen = dict(data), set()
        parent = data.get("Parent")
        while parent is not None:
            parent = resolve1(parent)
            if id(parent) in seen:
                raise ValueError("PDF 表单字段父级循环，无法解析")
            seen.add(id(parent))
            for key, value in parent.items():
                inherited.setdefault(key, value)
            parent = parent.get("Parent")
        name = value_text(inherited.get("T"))
        if name:
            value = value_text(inherited.get("V", data.get("AS")))
            fields.append(
                TextBox(f"{name}: {value}", annotation["x0"], annotation["top"], annotation["x1"], annotation["bottom"])
            )
    return fields


def render_layout(boxes, width, horizontal=(), vertical=()) -> str:
    """先组织规则表格及字段行，再按跨栏区域和栏内顺序输出。"""
    boxes = list(boxes)
    merged_vertical = []
    for x, top, bottom in sorted(vertical):
        match = next(
            (
                i
                for i, (old_x, old_top, old_bottom) in enumerate(merged_vertical)
                if abs(x - old_x) <= 3 and top <= old_bottom + 3 and bottom >= old_top - 3
            ),
            None,
        )
        if match is None:
            merged_vertical.append((x, top, bottom))
        else:
            old_x, old_top, old_bottom = merged_vertical[match]
            merged_vertical[match] = (old_x, min(top, old_top), max(bottom, old_bottom))
    vertical = merged_vertical
    tables = []
    rule_groups = []
    for x0, y, x1 in sorted(horizontal):
        if x1 - x0 < 70:
            continue
        group = next((g for g in rule_groups if abs(g[0][0] - x0) < 3 and abs(g[0][2] - x1) < 3), None)
        if group is None:
            rule_groups.append([(x0, y, x1)])
        elif all(abs(y - line[1]) > 2 for line in group):
            group.append((x0, y, x1))
    connected_groups = []
    for rules in rule_groups:
        remaining_rules = list(rules)
        # 两张同宽表不共享单元格；外框纵线确定各自的连通区域。
        for x, top, bottom in vertical:
            if abs(x - rules[0][0]) > 3 and abs(x - rules[0][2]) > 3:
                continue
            connected = [r for r in remaining_rules if top - 3 <= r[1] <= bottom + 3]
            if len(connected) >= 3:
                connected_groups.append(connected)
                remaining_rules = [r for r in remaining_rules if r not in connected]
        if remaining_rules:
            remaining_rules.sort(key=lambda r: r[1])
            start = 0
            for i in range(2, len(remaining_rules) - 3):
                lower, upper = remaining_rules[i][1], remaining_rules[i + 1][1]
                between = [
                    b
                    for b in boxes
                    if rules[0][0] <= b.x0 and b.x1 <= rules[0][2] and lower < (b.top + b.bottom) / 2 < upper
                ]
                if i - start >= 2 and not between:
                    connected_groups.append(remaining_rules[start : i + 1])
                    start = i + 1
            connected_groups.append(remaining_rules[start:])
    for rules in connected_groups:
        if len(rules) < 3:
            continue
        x0, x1 = rules[0][0], rules[0][2]
        ys = sorted(r[1] for r in rules)
        inside = [b for b in boxes if x0 - 3 <= b.x0 and b.x1 <= x1 + 3 and ys[0] <= (b.top + b.bottom) / 2 <= ys[-1]]
        if not inside:
            continue
        xs = sorted(
            {
                round(x, 1)
                for x, top, bottom in vertical
                if x0 - 2 <= x <= x1 + 2 and top <= ys[0] + 3 and bottom >= ys[-1] - 3
            }
        )
        partial_columns = [
            x
            for x, top, bottom in vertical
            if x0 + 3 < x < x1 - 3 and top < ys[-1] and bottom > ys[0] and (top > ys[0] + 3 or bottom < ys[-1] - 3)
        ]
        if partial_columns:
            raise ValueError("检测到非完整纵线或合并单元格，请使用专用版面解析引擎复核表格")
        if len(xs) >= 3:
            if any(b.x0 < x - 2 and b.x1 > x + 2 for b in inside for x in xs[1:-1]):
                raise ValueError("表格文字跨越列边界，无法可靠关联字段，请使用专用版面解析引擎")
            # 完整纵线定义单元格；空字段也保留，不能向相邻记录填充。
            rows = [
                [_cell_text(inside, left, top, right, bottom) for left, right in zip(xs, xs[1:])]
                for top, bottom in zip(ys, ys[1:])
            ]
        else:
            # 三线表按重复出现的列间隙定位；不确定结构时阻止扁平化入库。
            visual_rows = group_rows(inside)
            count = max(len(row) for row in visual_rows)
            complete_rows = [row for row in visual_rows if len(row) == count]
            if count < 2 or len(complete_rows) < 2:
                raise ValueError("检测到规则表格但无法确定列关系，请使用专用版面解析引擎")
            cuts = [median((row[i].x1 + row[i + 1].x0) / 2 for row in complete_rows) for i in range(count - 1)]
            if any(b.x0 < cut < b.x1 for b in inside for cut in cuts):
                raise ValueError("表格列对齐不明确，请使用专用版面解析引擎")
            limits = [x0 - 3, *cuts, x1 + 3]
            rows = []
            for row in visual_rows:
                cells = [
                    " ".join(b.text for b in row if left <= (b.x0 + b.x1) / 2 < right)
                    for left, right in zip(limits, limits[1:])
                ]
                if not cells[0] and rows:
                    for index, cell in enumerate(cells):
                        if cell:
                            rows[-1][index] += " " + cell
                else:
                    rows.append(cells)
        if len(rows) < 2:
            continue
        escaped = [[cell.replace("|", "\\|").replace("\n", "<br>") for cell in row] for row in rows]
        lines = ["| " + " | ".join(row) + " |" for row in escaped]
        lines.insert(1, "| " + " | ".join("---" for _ in rows[0]) + " |")
        tables.append(TextBox("\n".join(lines), x0, ys[0], x1, ys[-1]))
        boxes = [b for b in boxes if b not in inside]

    # 表单的显式标签与同一行的值保持相邻，不将其误判为两栏文章。
    fields = []
    for row in group_rows(boxes):
        if (
            len(row) > 1
            and len(row[0].text) <= 25
            and row[0].x1 - row[0].x0 < width * 0.25
            and row[0].text.rstrip().endswith((":", "："))
        ):
            fields.append(
                TextBox(
                    " ".join(b.text for b in row),
                    row[0].x0,
                    min(b.top for b in row),
                    row[-1].x1,
                    max(b.bottom for b in row),
                )
            )
            boxes = [b for b in boxes if b not in row]
    ordered = reading_order(boxes + tables + fields, width)
    return "\n\n".join(b.text for b in ordered).strip()


def group_rows(boxes) -> list[list[TextBox]]:
    """将坐标文本按视觉行分组，兼容同行的小号字符。"""
    rows = []
    for box in sorted(boxes, key=lambda b: ((b.top + b.bottom) / 2, b.x0)):
        if not rows or abs((box.top + box.bottom) / 2 - (rows[-1][0].top + rows[-1][0].bottom) / 2) > 5:
            rows.append([box])
        else:
            rows[-1].append(box)
    return [sorted(row, key=lambda b: b.x0) for row in rows]


def _cell_text(boxes, left, top, right, bottom) -> str:
    """按单元格坐标提取内容。"""
    selected = [b for b in boxes if left <= (b.x0 + b.x1) / 2 < right and top <= (b.top + b.bottom) / 2 < bottom]
    return " ".join(b.text for row in group_rows(selected) for b in row)


def reading_order(boxes, width) -> list[TextBox]:
    """跨栏区域分段；明显双栏区域先左后右，单栏保持行序。"""
    mid = width / 2
    gaps = [
        (left.x1, right.x0)
        for row in group_rows(boxes)
        for left, right in zip(row, row[1:])
        if right.x0 - left.x1 >= 6 and width * 0.3 < (left.x1 + right.x0) / 2 < width * 0.7
    ]
    if len(gaps) >= 2:
        # 短行的空白较宽，中点不是栏边界；优先选择最多行共同经过的空隙。
        mid = _column_gutter(gaps, width, 2) or mid
    spans = sorted([b for b in boxes if b.x0 < mid - 8 and b.x1 > mid + 8], key=lambda b: b.top)
    remaining = [b for b in boxes if b not in spans]
    result = []
    for span in [*spans, None]:
        section = [b for b in remaining if span is None or (b.top + b.bottom) / 2 < span.top]
        left = [b for b in section if (b.x0 + b.x1) / 2 < mid]
        right = [b for b in section if b not in left]
        if (
            left
            and right
            and len(section) >= 3
            and max(b.x1 for b in left) <= mid + 8
            and min(b.x0 for b in right) >= mid - 8
        ):
            result.extend(sorted(left, key=lambda b: (b.top, b.x0)))
            result.extend(sorted(right, key=lambda b: (b.top, b.x0)))
        else:
            result.extend(b for row in group_rows(section) for b in row)
        remaining = [b for b in remaining if b not in section]
        if span:
            result.append(span)
    return result
