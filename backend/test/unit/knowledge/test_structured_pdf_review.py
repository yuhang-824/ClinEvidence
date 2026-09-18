"""结构解析、原件不可变和审核拦截的独立证据。"""

import copy

import pytest

from yuxi.knowledge.chunking.mixed import chunk_mixed
from yuxi.knowledge.parser.docling_pdf import build_structure, check_native_coverage, split_text_sources
from yuxi.knowledge.structure import CHECKS, require_structure_review, revise_structure, structure_report


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


def test_native_coverage_flags_missing_text_and_scanned_page():
    """独立文本层恢复遗漏告警，扫描页不伪报百分百覆盖。"""
    structure = sample()
    check_native_coverage(structure, ["治疗 immutable 缺失"])
    assert structure["pages"][0]["native_text_check"]["missing_characters"] == 2
    assert any("缺少" in issue for issue in structure["pages"][0]["issues"])
    check_native_coverage(structure, [""])
    assert structure["pages"][0]["native_text_check"]["coverage"] is None


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
