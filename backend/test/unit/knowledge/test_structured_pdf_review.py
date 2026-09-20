"""结构解析、原件不可变和审核拦截的独立证据。"""

import copy

import pytest

from yuxi.knowledge.chunking.mixed import chunk_mixed
from yuxi.knowledge.parser.docling_pdf import build_structure, check_native_coverage, split_text_sources
from yuxi.knowledge.structure import (
    AUTO_REVIEW_MIN_COVERAGE,
    CHECKS,
    EMPTY_BLOCK_EXCLUDED_NOTE,
    PLACEHOLDER_EMPTY_BLOCK,
    coverage_shortfall,
    mark_auto_review,
    require_structure_review,
    revise_structure,
    structure_report,
)


def sample():
    """构造含原文、流程关系和已排除页脚的最小文档。"""
    return {
        "pages": [{"page": 1, "width": 600, "height": 800, "checks": dict.fromkeys(CHECKS, False), "issues": []}],
        "blocks": [
            {
                "id": "title",
                "page": 1,
                "kind": "heading",
                "text": "治疗",
                "source_text": "治疗",
                "bbox": [0, 0, 100, 20],
            },
            {
                "id": "flow",
                "page": 1,
                "kind": "relationship",
                "text": "条件 A → 检查 B\n\n阴性 → 随访 C",
                "source_text": "immutable",
                "bbox": [0, 30, 400, 600],
            },
        ],
    }


def payload(structure):
    """模拟客户端允许提交的字段。"""
    return {
        "blocks": [{k: b[k] for k in ("id", "page", "kind", "text")} for b in structure["blocks"]],
        "pages": [{"page": 1, "checks": dict.fromkeys(CHECKS, True), "note": "已核对 A 到 B、阴性到 C 的箭头"}],
    }


def page(number, **extra):
    """构造一页未核验的页面记录，可附加文本层比对证据。"""
    return {
        "page": number,
        "width": 600,
        "height": 800,
        "checks": dict.fromkeys(CHECKS, False),
        "note": "",
        "issues": [],
        **extra,
    }


def coverage_evidence(characters=100, absent_characters=0, visual=False):
    """构造独立文本层比对证据，coverage 由未覆盖比例派生。"""
    return {
        "characters": characters,
        "absent_characters": absent_characters,
        "absent_sample": "",
        "coverage": round(1 - absent_characters / characters, 4) if characters else None,
        "has_visual_content": visual,
    }


def test_review_gates_and_immutable_source():
    """修订不得伪造原件，未核验、未解释关系或失效来源不得审批。"""
    source = sample()
    _, initial = structure_report(source)
    with pytest.raises(ValueError, match="尚未完成"):
        require_structure_review(initial)
    change = payload(source)
    change["blocks"][1]["text"] += "（对照原图修订）"
    revised = revise_structure(source, change, "reviewer", "2026-09-18")
    assert revised["blocks"][1]["source_text"] == "immutable"
    content, report = structure_report(revised)
    assert require_structure_review(report) == content
    broken = copy.deepcopy(report)
    broken["structure"]["pages"][0]["note"] = ""
    with pytest.raises(ValueError, match="说明"):
        require_structure_review(broken)
    broken = copy.deepcopy(report)
    broken["block_spans"][0]["end"] += 1
    with pytest.raises(ValueError, match="来源映射"):
        require_structure_review(broken)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p["blocks"].pop(), "不能删除"),
        (lambda p: p["blocks"][0].update(bbox=[1, 2, 3, 4]), "不支持"),
        (lambda p: p["blocks"][0].update(page=2), "页码"),
        (lambda p: p["blocks"][0].update(excluded=True), "排除"),
        (lambda p: p["pages"][0]["checks"].update(text_complete="yes"), "分别核验"),
        (lambda p: p["pages"].clear(), "全部页面"),
        (lambda p: p["blocks"][0].update(id="unknown"), "未知"),
    ],
)
def test_reject_invalid_structure_edits(mutation, reason):
    """每种结构保护均拒绝可恢复原缺陷的负向输入。"""
    source = sample()
    change = payload(source)
    mutation(change)
    with pytest.raises(ValueError, match=reason):
        revise_structure(source, change, "reviewer", "now")


def test_mixed_keeps_relationship_and_attaches_source():
    """流程节点不能被空行拆开，标题随正文携带，来源可定位原页。"""
    source = sample()
    content, report = structure_report(source)
    chunks = chunk_mixed(content, "f", "f.pdf", {}, revision={"version": 1, "report": report})
    assert len(chunks) == 1
    assert source["blocks"][1]["text"] in chunks[0]["content"]
    assert "治疗" in chunks[0]["content"]
    assert chunks[0]["source_metadata"]["source_blocks"][-1]["bbox"] == [0, 30, 400, 600]
    report["chunk_boundaries"] = [content.index("阴性")]
    with pytest.raises(ValueError, match="关系"):
        chunk_mixed(content, "f", "f.pdf", {}, revision={"version": 1, "report": report})


def test_picture_children_and_orphan_text_are_retained():
    """阅读树遗漏的文字和图片子节点仍进入审核，不能静默丢失。"""
    prov = [{"page_no": 1, "bbox": {"l": 10, "r": 100, "t": 750, "b": 700, "coord_origin": "BOTTOMLEFT"}}]
    document = {
        "pages": {"1": {"size": {"width": 600, "height": 800}}},
        "body": {"children": [{"$ref": "picture"}]},
        "pictures": [{"self_ref": "picture", "label": "picture", "prov": prov, "children": [{"$ref": "inside"}]}],
        "texts": [
            {"self_ref": "inside", "label": "text", "prov": prov, "text": "阴性：随访"},
            {"self_ref": "orphan", "label": "text", "prov": prov, "text": "不应遗漏"},
        ],
    }
    structure = build_structure(document, {})
    assert [b["text"] for b in structure["blocks"]] == ["阴性：随访", "不应遗漏"]
    assert structure["blocks"][0]["bbox"] == [10, 50, 100, 100]
    assert any("主阅读树" in issue for issue in structure["pages"][0]["issues"])


def test_native_coverage_records_absent_characters_and_scanned_page():
    """独立文本层把解析结果完全找不到的连续片段记为缺失，扫描页不伪报百分百覆盖。"""
    structure = sample()
    # sample 的流程块 source_text 是 immutable，因此 治疗 与 immutable 都已被解析覆盖
    check_native_coverage(structure, ["治疗 immutable 缺失白细胞肾"])
    evidence = structure["pages"][0]["native_text_check"]
    assert (evidence["characters"], evidence["absent_characters"]) == (16, 6)
    assert evidence["absent_sample"] == "缺失白细胞肾"
    assert any("未出现在解析结果" in issue for issue in structure["pages"][0]["issues"])
    check_native_coverage(structure, [""])
    assert structure["pages"][0]["native_text_check"]["coverage"] is None


def test_native_coverage_checks_whole_document_and_normalizes_forms():
    """跨页续排的段落不算缺失；标记归一不误报，真正被丢弃的分期数字仍被发现。"""

    def evidence(native, parsed_blocks):
        structure = {
            "schema": 1,
            "parser": "test",
            "pages": [page(1), page(2)],
            "blocks": [
                {"id": f"b{i}", "page": p, "kind": "paragraph", "text": text, "source_text": text}
                for i, (p, text) in enumerate(parsed_blocks)
            ],
        }
        check_native_coverage(structure, [native, ""])
        return structure["pages"][0]["native_text_check"]

    # 段落从上一页续排：本页文本层的内容出现在上一页文块里，不算缺失
    sentence = "体外照射由CTV外放一定距离形成PTV，目前没有统一标准。"
    continued = evidence(sentence, [(1, "子宫颈癌" + sentence)])
    assert continued["absent_characters"] == 0
    # 原页 Ⅳ 与 ⑴ 在解析结果中写作 IV 与 (1)：同一内容，不报缺失
    normalized = evidence("治疗条件检查阴性随访Ⅳ期⑴", [(1, "治疗条件检查阴性随访IV期(1)")])
    assert (normalized["characters"], normalized["absent_characters"]) == (14, 0)
    # 解析结果真的丢了整句，必须被发现并给出原文片段
    lost = evidence("治疗条件检查阴性随访Ⅳ期", [(1, "治疗条件检查阴性随访期")])
    assert lost["absent_sample"] == "治疗条件检查阴性随访Ⅳ期"
    assert lost["absent_characters"] == 13
    # 表格 HTML 标签不参与比对：单元格文字按序拼起来仍然算已覆盖
    html = "<table><tr><td>药物</td><td>剂量</td></tr><tr><td>A</td><td>5mg</td></tr></table>"
    table = evidence("药物 剂量 A 5mg", [(1, html)])
    assert table["absent_characters"] == 0


def test_native_coverage_tolerance_is_ratio_based():
    """阈值按未覆盖比例判定：等于阈值放行，低于阈值保留人工核对项。"""
    parts = [chr(0x4E00 + i) + chr(0x5000 + i) for i in range(100)]
    parsed = "".join(parts)

    def check(extra):
        structure = {
            "schema": 1,
            "parser": "test",
            "pages": [page(1)],
            "blocks": [{"id": "b", "page": 1, "kind": "paragraph", "text": parsed, "source_text": parsed}],
        }
        check_native_coverage(structure, ["。".join([*parts, extra])])
        return structure["pages"][0]

    boundary = check("缺甲")
    assert boundary["native_text_check"]["absent_characters"] == 2
    assert boundary["native_text_check"]["coverage"] >= AUTO_REVIEW_MIN_COVERAGE
    assert not any("未出现在解析结果" in issue for issue in boundary["issues"])

    shortfall = check("缺甲乙丙")
    assert shortfall["native_text_check"]["absent_characters"] == 4
    assert shortfall["native_text_check"]["coverage"] < AUTO_REVIEW_MIN_COVERAGE
    assert any("未出现在解析结果" in issue for issue in shortfall["issues"])


def test_placeholder_block_cannot_be_indexed_without_text():
    """占位文块不能取消排除：没有正文就没有可索引内容，补充原文后才可以。"""
    structure = {
        "schema": 1,
        "parser": "docling",
        "pages": [page(1)],
        "blocks": [
            {
                "id": "ph",
                "page": 1,
                "kind": "paragraph",
                "text": PLACEHOLDER_EMPTY_BLOCK,
                "source_text": PLACEHOLDER_EMPTY_BLOCK,
                "excluded": True,
                "note": EMPTY_BLOCK_EXCLUDED_NOTE,
            }
        ],
    }

    def revise(text):
        return revise_structure(
            structure,
            {
                "blocks": [{"id": "ph", "page": 1, "kind": "paragraph", "text": text, "excluded": False, "note": ""}],
                "pages": [{"page": 1, "checks": dict.fromkeys(CHECKS, True), "note": "已核对"}],
            },
            "reviewer",
            "2026-09-20",
        )

    with pytest.raises(ValueError, match="占位文块"):
        revise(PLACEHOLDER_EMPTY_BLOCK)
    # 补充原文后参与检索，占位句不再出现
    content, _ = structure_report(revise("补录的原文"))
    assert content == "补录的原文"


def test_machine_review_clears_only_pages_without_anomalies():
    """机器核验只放行无异常、无表格图示且有独立比对的页，其余页保持人工。"""
    structure = {
        "schema": 1,
        "parser": "docling",
        "pages": [
            page(1, native_text_check=coverage_evidence()),
            page(2, native_text_check=coverage_evidence(characters=0)),
            page(3, native_text_check=coverage_evidence(absent_characters=40)),
            page(4, native_text_check=coverage_evidence()),
            page(5, native_text_check=coverage_evidence(characters=0)),
            page(6, native_text_check=coverage_evidence(characters=0, visual=True)),
            page(7, native_text_check=coverage_evidence(characters=0), issues=["文块有多处来源，请核对跨页关系"]),
            page(8, native_text_check=coverage_evidence(characters=0)),
            page(9, native_text_check=coverage_evidence(absent_characters=60)),
        ],
        "blocks": [
            {"id": "hdr", "page": 1, "kind": "paragraph", "text": "期刊名", "excluded": True},
            {"id": "body", "page": 1, "kind": "paragraph", "text": "正文"},
            {
                "id": "ph",
                "page": 2,
                "kind": "paragraph",
                "text": PLACEHOLDER_EMPTY_BLOCK,
                "excluded": True,
                "note": EMPTY_BLOCK_EXCLUDED_NOTE,
            },
            {"id": "gap", "page": 3, "kind": "paragraph", "text": "正文"},
            {"id": "tbl", "page": 4, "kind": "table", "text": "| a | b |"},
            {"id": "scan", "page": 5, "kind": "paragraph", "text": "扫描页正文"},
            {
                "id": "scan2",
                "page": 6,
                "kind": "paragraph",
                "text": PLACEHOLDER_EMPTY_BLOCK,
                "excluded": True,
                "note": EMPTY_BLOCK_EXCLUDED_NOTE,
            },
            {"id": "scan3", "page": 7, "kind": "paragraph", "text": "正文"},
            {
                "id": "scan4",
                "page": 8,
                "kind": "paragraph",
                "text": PLACEHOLDER_EMPTY_BLOCK,
                "excluded": True,
                "note": EMPTY_BLOCK_EXCLUDED_NOTE,
            },
            {"id": "short", "page": 9, "kind": "paragraph", "text": "正文"},
        ],
    }
    structure["pages"][0]["issues"].append("存在空结构块，请对照原文检查")
    structure["pages"][2]["issues"].append("独立文本层比对：40/100 个字符未出现在解析结果中")
    structure["pages"][4]["issues"].append("无可用 PDF 文本层，OCR 完整性须逐字对照原页")
    structure["pages"][5]["issues"].append("无可用 PDF 文本层，OCR 完整性须逐字对照原页")
    structure["pages"][5]["issues"].append("存在空结构块，请对照原文检查")
    structure["pages"][6]["issues"].append("无可用 PDF 文本层，OCR 完整性须逐字对照原页")
    structure["pages"][7]["issues"].append("无可用 PDF 文本层，OCR 完整性须逐字对照原页")
    # 第 8 页缺 has_visual_content 证据：不能判空白
    structure["pages"][7]["native_text_check"].pop("has_visual_content")
    mark_auto_review(structure)
    assert [p.get("auto_review", {}).get("rule") for p in structure["pages"]] == [
        "no-anomaly/v1",
        "blank-page/v1",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    # 空白页上由版面模型臆造的占位文块与提示被移除：页面只保留空白结论，不再像是内容被丢弃
    assert [b["id"] for b in structure["blocks"] if b["page"] == 2] == []
    assert structure["pages"][1]["issues"] == []
    # 有可视内容的页保留文块与提示（第 6 页有图像，不能当空白页丢内容）
    assert [b["id"] for b in structure["blocks"] if b["page"] == 6] == ["scan2"]
    # 规则自身校验覆盖比例：低覆盖且不带 issue 的页证据同样不能放行
    assert coverage_shortfall(structure["pages"][8]["native_text_check"])
    # 机器核验页不需要人工签核，其余页仍然被拦截
    with pytest.raises(ValueError, match="第 3 页尚未完成"):
        require_structure_review(structure_report(structure)[1])
    for number in (3, 4, 5, 6, 7, 8, 9):
        structure["pages"][number - 1].update(
            checks=dict.fromkeys(CHECKS, True), note="已对照原页核对", reviewed_by="reviewer"
        )
    assert require_structure_review(structure_report(structure)[1]) is not None


def test_machine_review_invalidated_by_human_edit():
    """人工修改正文、文块字段、页内顺序或页面说明后机器核验失效，未改动的页保留。"""
    source = {
        "schema": 1,
        "parser": "docling",
        "parser_version": "v1",
        "pages": [
            page(1, native_text_check=coverage_evidence()),
            page(2, native_text_check=coverage_evidence()),
        ],
        "blocks": [
            {"id": "a", "page": 1, "kind": "paragraph", "text": "甲"},
            {"id": "b", "page": 2, "kind": "paragraph", "text": "乙"},
            {"id": "c", "page": 2, "kind": "paragraph", "text": "丙"},
        ],
    }

    def revise(block_changes, page_notes=("", "已核对阅读顺序")):
        structure = copy.deepcopy(source)
        mark_auto_review(structure)
        assert all(p["auto_review"] for p in structure["pages"])
        return revise_structure(
            structure,
            {
                "blocks": block_changes,
                "pages": [
                    {"page": 1, "checks": dict.fromkeys(CHECKS, False), "note": page_notes[0]},
                    {"page": 2, "checks": dict.fromkeys(CHECKS, True), "note": page_notes[1]},
                ],
            },
            "reviewer",
            "2026-09-20",
        )

    untouched = revise(
        [
            {"id": "a", "page": 1, "kind": "paragraph", "text": "甲"},
            {"id": "b", "page": 2, "kind": "paragraph", "text": "乙"},
            {"id": "c", "page": 2, "kind": "paragraph", "text": "丙"},
        ]
    )
    assert untouched["pages"][0]["auto_review"]["rule"] == "no-anomaly/v1"
    assert untouched["pages"][0]["reviewed_by"] is None

    def blocks(overrides=None, extra=None):
        items = [
            {"id": "a", "page": 1, "kind": "paragraph", "text": "甲"},
            {"id": "b", "page": 2, "kind": "paragraph", "text": "乙"},
            {"id": "c", "page": 2, "kind": "paragraph", "text": "丙"},
        ]
        for index, values in (overrides or {}).items():
            items[index].update(values)
        return items + list(extra or [])

    edited = revise(blocks({0: {"text": "甲（人工修订）"}}))
    assert "auto_review" not in edited["pages"][0]
    with pytest.raises(ValueError, match="第 1 页尚未完成"):
        require_structure_review(structure_report(edited)[1])

    reclassified = revise(blocks({0: {"kind": "heading", "level": 3}}))
    assert "auto_review" not in reclassified["pages"][0]

    excluded = revise(blocks({0: {"excluded": True, "note": "人工排除"}}))
    assert "auto_review" not in excluded["pages"][0]

    added = revise(blocks(extra=[{"id": "new:1", "page": 2, "kind": "paragraph", "text": "补录"}]))
    assert added["pages"][1].get("auto_review") is None
    assert added["pages"][0]["auto_review"]["rule"] == "no-anomaly/v1"

    reordered = revise(
        [
            {"id": "a", "page": 1, "kind": "paragraph", "text": "甲"},
            {"id": "c", "page": 2, "kind": "paragraph", "text": "丙"},
            {"id": "b", "page": 2, "kind": "paragraph", "text": "乙"},
        ]
    )
    assert "auto_review" not in reordered["pages"][1]

    # 只写页面说明也是人工介入：机器结论不再覆盖该页，审阅者据此可以推翻机器判断
    noted = revise(
        [
            {"id": "a", "page": 1, "kind": "paragraph", "text": "甲"},
            {"id": "b", "page": 2, "kind": "paragraph", "text": "乙"},
            {"id": "c", "page": 2, "kind": "paragraph", "text": "丙"},
        ],
        page_notes=("本页机器判断有误，需重新解析", "已核对阅读顺序"),
    )
    assert "auto_review" not in noted["pages"][0]
    with pytest.raises(ValueError, match="第 1 页尚未完成"):
        require_structure_review(structure_report(noted)[1])


@pytest.mark.parametrize("line_count", [1, 2])
def test_manual_table_cut_cannot_detach_header(line_count):
    """人工切点不能分离列名、分隔行或产生只有表头的片段。"""
    table = "| 药物 | 剂量 |\n| --- | --- |\n| A | 5 mg |\n| B | 10 mg |"
    source = sample()
    source["blocks"] = [{"id": "table", "page": 1, "kind": "table", "text": table}]
    content, report = structure_report(source)
    report["chunk_boundaries"] = [sum(len(s) for s in content.splitlines(keepends=True)[:line_count])]
    with pytest.raises(ValueError, match="表头"):
        chunk_mixed(content, "f", "f.pdf", {}, revision={"version": 1, "report": report})


def test_cross_page_text_uses_each_source_page_and_box():
    """续页文字不得归到前页，原始 JSON 保持不变。"""
    boxes = [
        {"page_no": p, "charspan": span, "bbox": {"l": 10, "r": 100, "t": 20, "b": 50, "coord_origin": "TOPLEFT"}}
        for p, span in [(1, [0, 2]), (2, [3, 5])]
    ]
    original = {
        "pages": {str(p): {"size": {"width": 600, "height": 800}} for p in [1, 2]},
        "body": {"children": [{"$ref": "text"}]},
        "texts": [{"self_ref": "text", "text": "前页 后页", "label": "text", "prov": boxes}],
    }
    projected = build_structure(original, {})
    assert [(b["page"], b["text"]) for b in projected["blocks"]] == [(1, "前页"), (2, "后页")]
    assert len({b["id"] for b in projected["blocks"]}) == 2
    assert original["texts"][0]["text"] == "前页 后页"
    original["texts"][0]["prov"][1]["charspan"] = [1, 5]
    with pytest.raises(ValueError, match="重叠"):
        split_text_sources(original)


@pytest.mark.parametrize("spans,reason", [([[0, 2], [3, 99]], "无效"), ([[0, 1], [3, 5]], "无法定位")])
def test_cross_page_unlocatable_text_is_rejected(spans, reason):
    """越界和未覆盖正文不会产生看似完整的审核稿。"""
    original = {
        "texts": [
            {
                "self_ref": "t",
                "text": "前页 后页",
                "prov": [{"page_no": i + 1, "charspan": span} for i, span in enumerate(spans)],
            }
        ]
    }
    with pytest.raises(ValueError, match=reason):
        split_text_sources(original)


@pytest.mark.parametrize("kind", ["tables", "pictures"])
def test_non_text_multiple_sources_fail_explicitly(kind):
    """不猜测多来源表格和图片的字段归属。"""
    with pytest.raises(ValueError, match="多来源"):
        split_text_sources({kind: [{"prov": [{"page_no": 1}, {"page_no": 2}]}]})


def test_same_page_bad_charspan_keeps_whole_text_and_all_boxes():
    """复现实际样本文本归一化后范围过长，仍保留同页全文和全部坐标。"""
    sources = [
        {
            "page_no": 1,
            "charspan": span,
            "bbox": {"l": left, "r": left + 20, "t": 10, "b": 30, "coord_origin": "TOPLEFT"},
        }
        for left, span in [(10, [0, 2]), (100, [3, 99])]
    ]
    original = {
        "pages": {"1": {"size": {"width": 600, "height": 800}}},
        "texts": [{"self_ref": "t", "text": "完整正文", "label": "text", "prov": sources}],
    }
    projected = build_structure(original, {})
    assert projected["blocks"][0]["text"] == "完整正文"
    assert projected["blocks"][0]["source_provenance"] == sources
    assert projected["blocks"][0]["bbox"] == [10, 10, 120, 30]
