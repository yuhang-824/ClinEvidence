"""按文档结构切片，来源位置始终指向不可变的审核稿。"""

import hashlib
import re

from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens

HEADING = re.compile(r"^\s*(#{1,6})\s+\S")
RECOMMENDATION = re.compile(
    r"^\s*(?:\*\*|【)?(?:推荐(?:意见|及共识)?|共识|建议|Recommendation)\s*"
    r"(?:[:：]|[（(]?\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[）)]?\s*[:：.、])",
    re.I,
)
FIELD = re.compile(r"^\s*(?:[-*]\s+)?[^:：\n|，。；！？,.!?;]{1,30}[:：](?!//)")
TABLE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
CAPTION = re.compile(r"^\s*(?:表\s*\d+|Table\s+\d+)\b", re.I)
SENTENCE_END = re.compile(r"[。！？.!?][’”）)]*$")


def heading_level(line: str) -> int:
    """识别显式 Markdown 或参考文献标题。"""
    match = HEADING.match(line)
    if match:
        return len(match[1])
    return 1 if line.strip() in {"参考文献", "References"} else 0


def validate_boundaries(text: str, boundaries: list[int]) -> None:
    """人工切点必须严格递增且每段都有正文，不能删除或重复原文。"""
    if len(boundaries) > 10000:
        raise ValueError("人工切点不能超过 10000 个")
    previous = 0
    for point in [*boundaries, len(text)]:
        if type(point) is not int or not previous < point <= len(text) or not text[previous:point].strip():
            raise ValueError("切点须严格递增、位于正文内部，每个片段须有非空内容")
        previous = point


def chunk_mixed(text: str, file_id: str, filename: str, config: dict, *, revision=None) -> list[dict]:
    """保护结构单元并记录正文与重复上下文各自的字符位置。"""
    budget = int(config.get("chunk_token_num", 512))
    if not 64 <= budget <= 4096:
        raise ValueError("混合材料切片长度须在 64–4096 之间")
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    offsets.append(len(text))
    chunks, headings, table_headers = [], [], []
    page_spans = (revision or {}).get("report", {}).get("page_spans", [])
    block_spans = (revision or {}).get("report", {}).get("block_spans", [])

    def is_field(index):
        """冒号不是字段的充分证据，未完正文与跨行长值优先保持为段落。"""
        match = FIELD.match(lines[index])
        if not match:
            return False
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0:
            prior = lines[previous].strip()
            if not (FIELD.match(prior) or heading_level(prior) or SENTENCE_END.search(prior) or TABLE.match(prior)):
                return False
        value = lines[index][match.end() :].strip()
        following = index + 1
        while following < len(lines) and not lines[following].strip():
            following += 1
        if value and following < len(lines):
            next_text = lines[following]
            if not (
                FIELD.match(next_text)
                or heading_level(next_text)
                or TABLE.match(next_text)
                or RECOMMENDATION.match(next_text)
            ) and not SENTENCE_END.search(value):
                return False
        return not re.search(r"[，。；！？,;!?]", value) and len(value) <= 60

    def emit(start, end, kind, extra=()):
        """生成含上下文、可回读来源与质量提示的单一片段。"""
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            return
        if kind == "paragraph" and chunks:
            previous = chunks[-1]
            previous_meta = previous["source_metadata"]
            same_context = previous_meta["spans"][:-1] == [
                {"start": a, "end": b, "role": "context"} for _, a, b in headings
            ]
            if (
                previous_meta["kind"] == "paragraph"
                and same_context
                and not text[previous["end_char_pos"] : start].strip()
                and (
                    count_tokens(previous["content"] + text[start:end]) <= budget
                    or not SENTENCE_END.search(previous["content"])
                )
            ):
                chunks.pop()
                start = previous["start_char_pos"]
        spans = [{"start": a, "end": b, "role": "context"} for _, a, b in headings]
        spans += [{"start": a, "end": b, "role": "context"} for a, b in extra]
        spans.append({"start": start, "end": end, "role": "body"})
        content = "\n\n".join(text[s["start"] : s["end"]].strip() for s in spans)
        manual_plan = (revision or {}).get("report", {}).get("chunk_boundaries")
        if len(content.encode("utf-8")) > 60000 and (manual_plan is None or kind == "manual"):
            raise ValueError("存在超过存储上限的完整结构块，请在清洗稿中人工分段后重新预览")
        pages = sorted({p["page"] for p in page_spans if p["start"] < end and p["end"] > start})
        metadata = {
            "strategy": "mixed_v1",
            "revision": (revision or {}).get("version"),
            "kind": kind,
            "section": [text[a:b].strip().lstrip("# ") for _, a, b in headings],
            "spans": spans,
            "pages": pages,
            "source_blocks": [
                {k: block[k] for k in ("id", "page", "bbox")}
                for block in block_spans
                if any(block["start"] < s["end"] and block["end"] > s["start"] for s in spans)
            ],
            "start_line": text.count("\n", 0, start) + 1,
            "end_line": text.count("\n", 0, end) + 1,
            "language": "mixed"
            if re.search(r"[\u4e00-\u9fff]", content) and re.search(r"[A-Za-z]{2,}", content)
            else "zh"
            if re.search(r"[\u4e00-\u9fff]", content)
            else "en",
            "warnings": ["完整结构单元超过目标长度，已保留；请核对模型输入上限"]
            if count_tokens(content) > budget
            else [],
        }
        index = len(chunks)
        digest = hashlib.sha256((content + repr(spans)).encode()).hexdigest()[:16]
        chunk_id = f"{file_id}_m{digest}_{index}"
        chunks.append(
            {
                "id": chunk_id,
                "chunk_id": chunk_id,
                "file_id": file_id,
                "filename": filename,
                "source": filename,
                "chunk_index": index,
                "content": content,
                "start_char_pos": start,
                "end_char_pos": end,
                "source_metadata": metadata,
            }
        )

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        level = heading_level(line)
        protected = next((b for b in block_spans if b["start"] == offsets[i] and b["kind"] == "relationship"), None)
        if protected:
            emit(protected["start"], protected["end"], "relationship")
            while i < len(lines) and offsets[i] < protected["end"]:
                i += 1
            continue
        if level:
            headings = [h for h in headings if h[0] < level]
            emit(offsets[i], offsets[i + 1], "heading")
            headings.append((level, offsets[i], offsets[i + 1]))
            i += 1
            continue
        begin = i
        after_caption = i + 1
        while after_caption < len(lines) and not lines[after_caption].strip():
            after_caption += 1
        caption = CAPTION.match(line) and after_caption < len(lines) and TABLE.match(lines[after_caption])
        if TABLE.match(line) or caption:
            table_start = after_caption if caption else i
            i = table_start
            while i < len(lines) and TABLE.match(lines[i]):
                i += 1
            header_end = table_start + 2 if table_start + 1 < i and TABLE_RULE.match(lines[table_start + 1]) else None
            if header_end is None or header_end == i:
                emit(offsets[begin], offsets[i], "table")
                continue
            header = (offsets[begin], offsets[header_end])
            table_headers.append((header[0], header[1], offsets[i]))
            row_start = header_end
            for row in range(header_end, i):
                if (
                    row > row_start
                    and count_tokens(text[header[0] : header[1]] + text[offsets[row_start] : offsets[row + 1]]) > budget
                ):
                    emit(
                        offsets[begin] if row_start == header_end else offsets[row_start],
                        offsets[row],
                        "table",
                        [] if row_start == header_end else [header],
                    )
                    row_start = row
            emit(
                offsets[begin] if row_start == header_end else offsets[row_start],
                offsets[i],
                "table",
                [] if row_start == header_end else [header],
            )
            continue
        if line.lstrip().startswith(("```", "~~~", "<table")):
            fence = line.lstrip()[:3]
            i += 1
            while i < len(lines) and not (fence == "<ta" and "</table>" in line):
                closing = "</table>" in lines[i] if fence == "<ta" else lines[i].lstrip().startswith(fence)
                i += 1
                if closing:
                    break
            emit(offsets[begin], offsets[i], "table" if fence == "<ta" else "code")
            continue
        kind = "recommendation" if RECOMMENDATION.match(line) else "form" if is_field(i) else "paragraph"
        i += 1
        while i < len(lines):
            following = lines[i]
            if any(b["start"] == offsets[i] and b["kind"] == "relationship" for b in block_spans):
                break
            if not following.strip() and kind == "recommendation":
                next_line = i + 1
                while next_line < len(lines) and not lines[next_line].strip():
                    next_line += 1
                next_text = lines[next_line] if next_line < len(lines) else ""
                continuation = FIELD.match(next_text) or re.match(
                    r"\s*(?:推荐|建议|不建议|禁忌|适用|Recommendation)", next_text, re.I
                )
                if SENTENCE_END.search(text[offsets[begin] : offsets[i]].rstrip()) and not continuation:
                    break
            if not following.strip() and kind != "recommendation":
                previous_text = text[offsets[begin] : offsets[i]].rstrip()
                unfinished_prose = kind == "paragraph" and not SENTENCE_END.search(previous_text)
                empty_field = kind == "form" and previous_text.endswith((":", "："))
                if not (unfinished_prose or empty_field):
                    break
            if heading_level(following) or TABLE.match(following) or RECOMMENDATION.match(following):
                break
            if following.lstrip().startswith(("```", "~~~", "<table")):
                break
            if CAPTION.match(following):
                break
            if kind == "paragraph" and is_field(i):
                break
            i += 1
        start, end = offsets[begin], offsets[i]
        if kind != "paragraph" or count_tokens(text[start:end]) <= budget:
            emit(start, end, kind)
            continue
        # 小数、缩写和数值单位不按字符硬拆；无法安全分句的长句保留并提示。
        boundaries = [start]
        for match in re.finditer(r"[。！？](?:[’”）])?|[.!?](?=\s+[A-Z])", text[start:end]):
            pos = start + match.end()
            if re.search(r"\b(?:Dr|Mr|Mrs|Ms|Prof|Fig|No|vs|etc|e\.g|i\.e)\.$", text[start:pos], re.I):
                continue
            boundaries.append(pos)
        boundaries.append(end)
        segment = start
        for left, right in zip(boundaries, boundaries[1:]):
            if left > segment and count_tokens(text[segment:right]) > budget:
                emit(segment, left, kind)
                segment = left
        emit(segment, end, kind)
    if not chunks and text.strip():
        headings = []
        emit(0, len(text), "paragraph")
    # 已作为正文上下文携带的标题不再成为独立检索碎片，孤立标题仍保留。
    carried = {
        (s["start"], s["end"])
        for c in chunks
        if c["source_metadata"]["kind"] != "heading"
        for s in c["source_metadata"]["spans"]
        if s["role"] == "context"
    }
    chunks = [
        c
        for c in chunks
        if c["source_metadata"]["kind"] != "heading"
        or not any(a <= c["start_char_pos"] and c["end_char_pos"] <= b for a, b in carried)
    ]
    boundaries = (revision or {}).get("report", {}).get("chunk_boundaries")
    if boundaries is not None:
        validate_boundaries(text, boundaries)
        for point in boundaries:
            if any(start < point <= header_end for start, header_end, _ in table_headers):
                raise ValueError("表头和分隔行不能单独切片，请在完整数据行之后切分")
            for block in block_spans:
                if block["start"] < point < block["end"]:
                    if block["kind"] == "relationship":
                        raise ValueError("流程关系单元不能从中间切开，请在结构审核中修订关系")
                    if block["kind"] == "table" and text[point - 1] != "\n":
                        raise ValueError("结构表格只能在完整行之间切分")
        automatic, chunks = chunks, []
        for start, end in zip([0, *boundaries], [*boundaries, len(text)]):
            headings = []
            for n, line in enumerate(lines):
                level = heading_level(line)
                if offsets[n] >= start:
                    break
                if level and offsets[n + 1] <= start:
                    headings = [h for h in headings if h[0] < level]
                    headings.append((level, offsets[n], offsets[n + 1]))
            # 人工拆分长表时沿用覆盖起点的表头；位于正文内的上下文无需重复。
            owner = next((c for c in automatic if c["start_char_pos"] <= start < c["end_char_pos"]), None)
            contexts = (owner or {}).get("source_metadata", {}).get("spans", [])
            extra = [
                (s["start"], s["end"])
                for s in contexts
                if s["role"] == "context"
                and s["end"] <= start
                and (s["start"], s["end"]) not in [(a, b) for _, a, b in headings]
            ]
            for table_start, header_end, table_end in table_headers:
                if header_end <= start < table_end and (table_start, header_end) not in extra:
                    extra.append((table_start, header_end))
            emit(start, end, "manual", extra)
    for index, chunk in enumerate(chunks):
        chunk["chunk_index"] = index
        chunk["id"] = chunk["chunk_id"] = chunk["id"].rsplit("_", 1)[0] + f"_{index}"
    return chunks
