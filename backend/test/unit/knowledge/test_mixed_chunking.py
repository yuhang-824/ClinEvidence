"""用明确结构和原文字符核验混合材料切片。"""

import pytest

from yuxi.knowledge.chunking.mixed import chunk_mixed
from yuxi.knowledge.cleaning import PAGE_BREAK, clean_document
from yuxi.knowledge.utils.kb_utils import resolve_processing_params


def test_tables_repeat_header_preserve_rows_and_exact_source():
    """长表逐行切分，每块保留章节与表头，原字段不改写。"""
    rows = [f"| 药物{i} | 不超过 5 mg/kg，每 3 周给药；肾功能不全禁用 |" for i in range(15)]
    text = "# 治疗\n\n表1 用药\n| 药物 | 剂量与限制 |\n| --- | --- |\n" + "\n".join(rows)
    chunks = chunk_mixed(text, "file", "fixture.pdf", {"chunk_token_num": 64}, revision={"version": 2})
    tables = [c for c in chunks if c["source_metadata"]["kind"] == "table"]
    assert len(tables) > 1
    for chunk in tables:
        assert "# 治疗" in chunk["content"] and "| 药物 | 剂量与限制 |" in chunk["content"]
        assert "表1 用药" in chunk["content"]
        metadata = chunk["source_metadata"]
        assert metadata["revision"] == 2
        reconstructed = "\n\n".join(text[s["start"] : s["end"]].strip() for s in metadata["spans"])
        assert reconstructed == chunk["content"]
    for row in rows:
        assert sum(row in c["content"] for c in tables) == 1


def test_forms_recommendations_and_english_medical_units():
    """字段组与超长推荐不拆，英文长段按句切，不破坏小数与单位。"""
    form = "姓名：测试\n诊断：卵巢癌\n剂量：\n5 mg/kg\n禁忌：严重肾损害"
    recommendation = "推荐意见1：" + "不建议在缺乏证据时增加剂量。" * 20
    sentence = "Dr. Smith advises 2.5 mg/kg every three weeks. "
    text = "# 中文\n\n" + form + "\n\n" + recommendation + "\n\n# English\n\n" + sentence * 25
    chunks = chunk_mixed(text, "f", "f.pdf", {"chunk_token_num": 64})
    assert any(form in c["content"] and c["source_metadata"]["kind"] == "form" for c in chunks)
    recommended = [c for c in chunks if c["source_metadata"]["kind"] == "recommendation"]
    assert len(recommended) == 1 and recommendation in recommended[0]["content"]
    assert recommended[0]["source_metadata"]["warnings"]
    english = [c for c in chunks if c["source_metadata"]["kind"] == "paragraph"]
    assert len(english) > 1
    for c in english:
        body = c["source_metadata"]["spans"][-1]
        assert text[body["start"] : body["end"]].startswith("Dr. Smith")
        assert text[body["start"] : body["end"]].endswith("weeks.")


def test_page_mapping_only_when_available_and_manual_version_unknown():
    """页码来源是解析边界；历史或人工稿不猜页码。"""
    cleaned, report = clean_document("第一页：否认过敏" + PAGE_BREAK + "第二页：5 mg/kg")
    chunks = chunk_mixed(cleaned, "f", "f.pdf", {}, revision={"version": 1, "report": report})
    assert [c["source_metadata"]["pages"] for c in chunks] == [[1], [2]]
    manual = chunk_mixed(cleaned, "f", "f.pdf", {}, revision={"version": 2, "report": {"manual": True}})
    assert all(c["source_metadata"]["pages"] == [] for c in manual)


@pytest.mark.parametrize("size", [0, 63, 4097])
def test_invalid_budget_rejected(size):
    """非法长度不能静默采用另一套规则。"""
    with pytest.raises(ValueError, match="64"):
        chunk_mixed("text", "f", "f.pdf", {"chunk_token_num": size})


def test_over_storage_limit_fails_before_indexing():
    """超大字段不给出看似成功但无法存储的片段。"""
    with pytest.raises(ValueError, match="存储上限"):
        chunk_mixed("病史：" + "字" * 22000, "f", "f.pdf", {})


def test_html_table_and_following_paragraph_both_preserved():
    """单行 HTML 表格不吞掉后文。"""
    text = "<table><tr><td>5 mg/kg</td></tr></table>\n\nFollow-up required."
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    assert len(chunks) == 2
    assert chunks[0]["source_metadata"]["kind"] == "table"
    assert chunks[1]["content"] == "Follow-up required."


def test_previous_preview_version_not_inherited():
    """版本条件属于本次请求，不阻止之后审核的新版本入库。"""
    assert "review_version" not in resolve_processing_params({}, {"review_version": 1}, {})
    assert resolve_processing_params({}, {}, {"review_version": 2})["review_version"] == 2


def test_recommendation_conditions_remain_with_recommendation():
    """条件字段不是独立表单，不可脱离推荐句。"""
    text = "推荐意见1：可以使用方案A。\n适用人群：复发患者\n禁忌：严重肾损害\n推荐意见2：不建议加量。"
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    assert len(chunks) == 2
    assert "禁忌：严重肾损害" in chunks[0]["content"]
    assert "推荐意见2" not in chunks[0]["content"]


def test_caption_without_blank_line_attaches_to_table():
    """表前没有空行也不能把表名吞入上一正文。"""
    text = "Some background\nTable 1 Drugs\n| Drug | Dose |\n| --- | --- |\n| A | 5 mg/kg |"
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    assert chunks[0]["content"] == "Some background"
    assert "Table 1 Drugs" in chunks[1]["content"]


def test_pdf_blank_lines_do_not_fragment_recommendation_or_prose():
    """逐行带空行的 PDF 保留完整推荐，普通正文按预算组合。"""
    text = (
        "# 1 评估\n\n普通正文第一行\n\n普通正文第二行\n\n推荐及共识：仅限满足条件的\n\n"
        "患者，不建议加量（推荐级别：2A类）。\n\n# 2 治疗\n\n表1 用药\n\n| 药 | 剂量 |\n| --- | --- |\n| A | 5 mg/kg |"
    )
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    recs = [c for c in chunks if c["source_metadata"]["kind"] == "recommendation"]
    assert len(recs) == 1 and "2A类" in recs[0]["content"] and "2 治疗" not in recs[0]["content"]
    assert any("普通正文第一行\n\n普通正文第二行" in c["content"] for c in chunks)
    assert "表1 用药" in chunks[-1]["content"]


def test_rechunking_changes_identity_when_source_ranges_change():
    """同版本不同参数不能让旧卡片读取同 ID 的新片段来源。"""
    text = "Dr. Smith advises 5 mg/kg weekly. " * 40
    short = chunk_mixed(text, "f", "f.pdf", {"chunk_token_num": 64})
    long = chunk_mixed(text, "f", "f.pdf", {"chunk_token_num": 512})
    assert short[0]["id"] != long[0]["id"]


def test_recommendation_label_not_confused_with_wrapped_text():
    """换行后的推荐级别不是新推荐，年份引用不是推荐编号。"""
    text = "推荐及共识：不作常规\n\n推荐（推荐级别：3类）。\n\n# 2 随访\n\n共识（2022年版）已发表。"
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    recs = [c for c in chunks if c["source_metadata"]["kind"] == "recommendation"]
    assert len(recs) == 1 and "3类" in recs[0]["content"]


@pytest.mark.parametrize("dose", ["5 IU", "10 days", "5 毫克", "2.5 mg/kg"])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_dose_value_is_never_a_heading_inside_form(dose, separator):
    """字段空值后的数值行仍属于字段，不成为后续片段的标题。"""
    text = f"Dose:{separator}{dose}\nContraindication: severe renal impairment\n\nFollow-up monitoring is required."
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    assert chunks[0]["source_metadata"]["kind"] == "form"
    assert "Dose:" in chunks[0]["content"] and dose in chunks[0]["content"]
    assert all(c["source_metadata"]["kind"] != "heading" for c in chunks)
    assert dose not in chunks[-1]["content"]


@pytest.mark.parametrize("dose", ["5 IU", "2 IU"])
def test_numbered_text_does_not_turn_recommendation_dose_into_title(dose):
    """连续编号本身不构成标题证据，恰好同序号的剂量也保持关联。"""
    text = (
        f"1 Assessment\n\nRecommendation: Administer\n\n{dose}\n\nonly to eligible adults.\n\n"
        "2 Treatment\n\nTreatment text.\n\n3 Follow-up\n\nFollow-up text."
    )
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    rec = next(c for c in chunks if c["source_metadata"]["kind"] == "recommendation")
    assert dose in rec["content"] and "only to eligible adults." in rec["content"]
    assert "2 Treatment" not in rec["content"]
    assert all(dose not in c["source_metadata"]["section"] for c in chunks)


@pytest.mark.parametrize(
    "text",
    [
        "DESKTOP-Ⅰ试验探索并建立了\n\nAGO评分阳性标准：ECOG 0分，初次手术无肉眼残留病灶\n\n（R0）切除，无腹水或腹水≤500mL。",
        "入选标\n\n准包括：PFI超过6个月；初始治疗接受含铂化疗时，需在最\n\n后3个疗程化疗时达到完全缓解。",
        "该试验同时探\n\n索性研究了两个问题：（1）手术能否给患者带来\n\n生存获益。（2）联合方案的疗效。",
        "符合条件时可以\n\n考虑实施SCS：（1）初次化疗结束后超过６个月。（2）患者一\n\n般情况好。（3）无腹水。",
    ],
)
def test_wrapped_prose_colons_do_not_create_incomplete_form_fragments(text):
    """实际缺陷形态：冒号附近的跨行句子必须同片，不只验证字数。"""
    chunks = chunk_mixed(text, "f", "f.pdf", {"chunk_token_num": 64})
    assert len(chunks) == 1
    assert chunks[0]["content"] == text
    assert chunks[0]["source_metadata"]["kind"] == "paragraph"


def test_manual_boundaries_partition_unicode_and_keep_version_source():
    """人工合并拆分使用 Unicode 字符，正文恰好覆盖一次并回指版本。"""
    text = "# 标题\n\n第一句😀。\n\n第二句。"
    cut = text.index("第二")
    revision = {
        "version": 3,
        "report": {"chunk_boundaries": [cut], "page_spans": [{"page": 1, "start": 0, "end": len(text)}]},
    }
    chunks = chunk_mixed(text, "f", "f.pdf", {}, revision=revision)
    assert len(chunks) == 2
    assert chunks[0]["content"] == text[:cut].strip()
    assert chunks[1]["content"] == "# 标题\n\n第二句。"
    assert all(c["source_metadata"]["revision"] == 3 for c in chunks)
    assert all(c["source_metadata"]["pages"] == [1] for c in chunks)
    assert all(c["source_metadata"]["kind"] == "manual" for c in chunks)


@pytest.mark.parametrize("cuts", [[0], [20], [5, 5], [6, 3], [True], [2]])
def test_invalid_manual_boundaries_fail(cuts):
    """乱序、越界和空白片段不能保存为人工方案。"""
    with pytest.raises(ValueError, match="切点"):
        chunk_mixed("  abcdef", "f", "f.pdf", {}, revision={"report": {"chunk_boundaries": cuts}})


def test_short_colon_value_with_continuation_is_prose():
    """独立段落开头的冒号也不能截断后面的条件。"""
    text = "前一段已结束。\n\n入选标准：PFI超过6个月\n\n且初始治疗接受含铂化疗。"
    chunks = chunk_mixed(text, "f", "f.pdf", {})
    assert any("入选标准：PFI超过6个月\n\n且初始治疗接受含铂化疗。" in c["content"] for c in chunks)
    assert all(c["source_metadata"]["kind"] != "form" for c in chunks)


def test_manual_split_small_table_repeats_header():
    """自动仅一片的小表，人工拆行后每片仍有表头。"""
    text = "表1 用药\n| 药物 | 剂量 |\n| --- | --- |\n| A | 5 mg |\n| B | 10 mg |"
    chunks = chunk_mixed(text, "f", "f.pdf", {}, revision={"report": {"chunk_boundaries": [text.index("| B")]}})
    assert len(chunks) == 2
    assert all("| 药物 | 剂量 |" in c["content"] for c in chunks)
    assert "| A | 5 mg |" not in chunks[1]["content"]


def test_manual_can_split_oversized_structure():
    """已存在的超大结构仍可通过人工切点修复。"""
    text = "病史：" + "字" * 22000
    chunks = chunk_mixed(text, "f", "f.pdf", {}, revision={"report": {"chunk_boundaries": [11000]}})
    assert len(chunks) == 2
    with pytest.raises(ValueError, match="存储上限"):
        chunk_mixed(text, "f", "f.pdf", {}, revision={"report": {"chunk_boundaries": []}})
