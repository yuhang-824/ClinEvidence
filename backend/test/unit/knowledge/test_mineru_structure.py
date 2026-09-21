"""用真实 MinerU 官方 API 输出形态核验结构适配器。

fixture 内容为合成文本，字段形状取自 mineru.net 解析结果：
layout.json 的 pdf_info（para_blocks 含 title/text/table 与 level/index）、
discarded_blocks（页眉/页脚）、content_list（表格 table_body HTML）。
"""

import pytest

from yuxi.knowledge.parser.mineru_structure import build_structure_from_mineru
from yuxi.knowledge.structure import structure_report

LAYOUT = {
    "_version_name": "mineru2.5-2509-1.2.0",
    "pdf_info": [
        {
            "page_idx": 0,
            "page_size": [595, 842],
            "para_blocks": [
                {
                    "type": "title",
                    "bbox": [70, 84, 230, 103],
                    "level": 2,
                    "index": 2,
                    "lines": [{"spans": [{"content": "1 共识制定方法及流程"}]}],
                },
                {
                    "type": "title",
                    "bbox": [70, 118, 165, 132],
                    "level": 2,
                    "index": 3,
                    "lines": [{"spans": [{"content": "1.1 确定临床问题"}]}],
                },
                {
                    "type": "text",
                    "bbox": [69, 146, 493, 177],
                    "index": 4,
                    "lines": [
                        {"spans": [{"content": "在确定临床问题及编写共识的过程中，首先由专家组成员提出临床问题。"}]}
                    ],
                },
                {
                    "type": "table",
                    "bbox": [70, 237, 372, 289],
                    "index": 5,
                    "blocks": [
                        {"type": "table_body", "bbox": [70, 237, 372, 289], "lines": []}
                    ],
                },
            ],
            "discarded_blocks": [
                {
                    "type": "header",
                    "bbox": [70, 30, 237, 42],
                    "lines": [{"spans": [{"content": "中国实用妇科与产科杂志 2026 年 9 月"}]}],
                },
                {
                    "type": "page_number",
                    "bbox": [284, 791, 300, 801],
                    "lines": [{"spans": [{"content": "937"}]}],
                },
            ],
        },
        {
            "page_idx": 1,
            "page_size": [595, 842],
            "para_blocks": [],
            "discarded_blocks": [],
        },
    ],
}

CONTENT_LIST = [
    {"type": "text", "text": "1 共识制定方法及流程", "bbox": [117, 102, 211, 119], "page_idx": 0},
    {"type": "text", "text": "1.1 确定临床问题", "bbox": [115, 140, 191, 156], "page_idx": 0},
    {
        "type": "text",
        "text": "在确定临床问题及编写共识的过程中，首先由专家组成员提出临床问题。",
        "bbox": [115, 172, 692, 188],
        "page_idx": 0,
    },
    {
        "type": "table",
        "img_path": "images/a.jpg",
        "table_caption": ["表 1 推荐等级"],
        "table_body": "<table><tr><td>药物</td><td>剂量</td></tr></table>",
        "bbox": [117, 281, 625, 343],
        "page_idx": 0,
    },
    {"type": "header", "text": "中国实用妇科与产科杂志 2026 年 9 月", "page_idx": 0},
    {"type": "page_number", "text": "937", "page_idx": 0},
]


@pytest.fixture()
def structure():
    return build_structure_from_mineru(LAYOUT, CONTENT_LIST, source_sha256="abc123")


def test_pages_from_layout_page_size_and_issues_deduped(structure):
    assert structure["schema"] == 1
    assert structure["parser"] == "mineru"
    assert structure["source_sha256"] == "abc123"
    assert [(p["page"], p["width"], p["height"]) for p in structure["pages"]] == [
        (1, 595.0, 842.0),
        (2, 595.0, 842.0),
    ]
    assert all(not p["checks"]["reading_order"] for p in structure["pages"])
    assert "页面没有正文文块：确认空白页或补录遗漏内容" in structure["pages"][1]["issues"]


def test_titles_map_to_heading_with_numbering_level(structure):
    headings = [b for b in structure["blocks"] if b["kind"] == "heading"]
    assert [(b["text"], b["level"]) for b in headings] == [
        ("1 共识制定方法及流程", 1),
        ("1.1 确定临床问题", 2),
    ]
    assert all(b["source_label"] == "title" for b in headings)


def test_table_text_uses_content_list_html_by_page_order(structure):
    tables = [b for b in structure["blocks"] if b["kind"] == "table"]
    assert len(tables) == 1
    assert tables[0]["text"] == "<table><tr><td>药物</td><td>剂量</td></tr></table>"


def test_discarded_blocks_surface_as_excluded_for_review(structure):
    excluded = [b for b in structure["blocks"] if b["excluded"]]
    assert [b["text"] for b in excluded] == [
        "中国实用妇科与产科杂志 2026 年 9 月",
        "937",
    ]
    assert all(
        b["note"] == "解析器判定为页眉/页脚或噪声，请人工确认排除" for b in excluded
    )


def test_blocks_sorted_by_page_and_ids_are_stable(structure):
    pages = [b["page"] for b in structure["blocks"]]
    assert pages == sorted(pages)
    body_ids = [b["id"] for b in structure["blocks"] if not b["excluded"]]
    assert body_ids[:4] == ["m0_2", "m0_3", "m0_4", "m0_5"]


def test_missing_content_list_falls_back_to_placeholder_table():
    structure = build_structure_from_mineru(LAYOUT, [])
    tables = [b for b in structure["blocks"] if b["kind"] == "table"]
    assert "请对照原页核对行列与数值" in tables[0]["text"]
    assert any("表格内容未能自动提取" in issue for issue in structure["pages"][0]["issues"])


def test_table_alignment_requires_matching_type_sequence():
    """计数相同但页内类型顺序不一致时不按位置填内容，降级占位避免张冠李戴。"""
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [595, 842],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [70, 84, 230, 103],
                        "index": 0,
                        "lines": [{"spans": [{"content": "甲段"}]}],
                    },
                    {
                        "type": "table",
                        "bbox": [70, 137, 372, 189],
                        "index": 1,
                        "blocks": [{"type": "table_body", "bbox": [70, 137, 372, 189], "lines": []}],
                    },
                ],
                "discarded_blocks": [],
            }
        ]
    }
    # content_list 顺序与版面块相反：表格在前、正文在后
    content_list = [
        {
            "type": "table",
            "table_body": "<table><tr><td>乙表</td></tr></table>",
            "page_idx": 0,
        },
        {"type": "text", "text": "甲段", "page_idx": 0},
    ]
    structure = build_structure_from_mineru(layout, content_list)
    tables = [b for b in structure["blocks"] if b["kind"] == "table"]
    assert len(tables) == 1
    assert "乙表" not in tables[0]["text"]
    assert "请对照原页核对行列与数值" in tables[0]["text"]
    assert any("表格内容未能自动提取" in issue for issue in structure["pages"][0]["issues"])


def test_empty_block_is_excluded_so_placeholder_never_reaches_content():
    """解析器没取到文字的空块直接排除，占位正文不进入审核稿正文。"""
    layout = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [595, 842],
                "para_blocks": [
                    {"type": "text", "bbox": [70, 84, 230, 103], "index": 0, "lines": []},
                    {
                        "type": "text",
                        "bbox": [70, 120, 230, 140],
                        "index": 1,
                        "lines": [{"spans": [{"content": "该页正文"}]}],
                    },
                ],
                "discarded_blocks": [],
            }
        ]
    }
    structure = build_structure_from_mineru(layout, [])
    empty, body = structure["blocks"]
    assert empty["excluded"] is True
    assert empty["note"] == "解析器未取到该区域文字，已排除；原页如有内容请补充后取消排除"
    assert body["excluded"] is False
    # 空块不再产生页面级 issue：说明与排除标记落在文块自身
    assert structure["pages"][0]["issues"] == []

    content, report = structure_report(structure)
    assert "空结构块" not in content
    assert "该页正文" in content
    assert [b["id"] for b in report["structure"]["blocks"] if not b["excluded"]] == ["m0_1"]
