"""清洗规则必须保留医学信息和结构。"""

from yuxi.knowledge.cleaning import PAGE_BREAK, clean_document
import pytest


@pytest.mark.parametrize("prefix", ["- ", "1. ", "> ", "## ", "剂量：5 mg，", "5毫克"])
def test_no_join_after_structural_or_medical_line(prefix):
    raw = prefix + "不建议使用阿司匹林\n建议使用对乙酰氨基酚"
    assert clean_document(raw)[0] == raw


def test_remove_only_identified_repeated_margin_and_keep_medical_values():
    body = "推荐：不建议使用 5 mg/kg；P < 0.05，95% CI 1.2–3.4。\n| 剂量 | 单位 |\n| --- | --- |\n| 5 | mg/kg |"
    raw = PAGE_BREAK.join(f"中华测试杂志 2023年9月 第39卷 第9期\n{body}\n第 {n} 页" for n in range(1, 4))
    cleaned, report = clean_document(raw)
    assert "中华测试杂志" not in cleaned
    assert "第 1 页" not in cleaned
    assert cleaned.count(body) == 3
    assert len([c for c in report["changes"] if c["kind"] == "margin"]) == 6


def test_repeated_body_numeric_cells_and_form_labels_are_never_deleted():
    body = "禁止使用\n患者：张某\n| 2023 | 5 |\n| --- | --- |\n| | 2 |\n2023"
    cleaned, _ = clean_document(PAGE_BREAK.join([body] * 3))
    for value in ("禁止使用", "患者：张某", "| | 2 |", "2023"):
        assert cleaned.count(value) == body.count(value) * 3


def test_unpaged_document_preserves_repeated_headers_and_english_hyphens():
    raw = "Journal of Test\nnon-\nsmall cell lung cancer\nJournal of Test\n-5 mg\n"
    cleaned, report = clean_document(raw)
    assert cleaned == raw.strip()
    assert report["warnings"][0]["code"] == "no_page_boundaries"


def test_quality_flags_blank_page_unknown_character_and_malformed_table():
    raw = PAGE_BREAK.join(["", "| 名称 | 剂量 |\n| 药品 |\n乱码\ufffd"])
    _, report = clean_document(raw)
    assert {w["code"] for w in report["warnings"]} >= {"sparse_page", "unrecognized", "table_columns"}


def test_join_chinese_wrap_preserves_recommendation_and_unit_lines():
    raw = "患者存在持续性\n临床症状。\n推荐：保留\n5 mg/kg\n不建议\n"
    cleaned, report = clean_document(raw)
    assert cleaned == "患者存在持续性临床症状。\n推荐：保留\n5 mg/kg\n不建议"
    assert [c["kind"] for c in report["changes"]] == ["join_line"]


def test_running_journal_header_inside_column_order_and_page_marker():
    body = "left text\n" * 6 + "中国测试杂志 2023年9月 第39卷 第9期\n·935·\n" + "right text\n" * 6
    reference = "文献［1］发表于中国测试杂志，2023年9月 第39卷 第9期"
    cleaned, _ = clean_document(PAGE_BREAK.join([body + reference] * 3))
    assert "\n中国测试杂志" not in cleaned and "·935·" not in cleaned
    assert cleaned.count(reference) == 3


def test_page_text_drop_flags_possible_missing_column_without_deleting_content():
    pages = ["完整正文。" * 100, "本页内容。" * 12, "完整正文。" * 100]
    cleaned, report = clean_document(PAGE_BREAK.join(pages))
    assert cleaned == "\n\n".join(pages)
    assert any(w["code"] == "page_text_drop" and w["page"] == 2 for w in report["warnings"])


def test_fenced_content_is_not_normalized_or_joined():
    content = "```text\n连续中文\n续行中文  \n\n\n```"
    assert clean_document(content)[0] == content


def test_journal_prefix_with_medical_content_is_never_a_header():
    body = "Journal recommendation: do not exceed 5 mg/kg\n中华测试杂志建议：不超过5毫克\n正文内容。"
    cleaned, _ = clean_document(PAGE_BREAK.join([body] * 3))
    assert cleaned == "\n\n".join([body] * 3)
