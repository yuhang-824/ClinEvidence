"""用独立布局样例验证顺序、表格和字段关系，不调用真实 OCR 模型。"""

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

from yuxi.knowledge.parser.pdf_layout import TextBox, image_rules, render_layout
from yuxi.knowledge.parser.rapid_ocr import RapidOCRParser


def build_layout_pdf(path, narrow=False):
    """生成固定双栏、表格和字段样例，预期内容由测试独立声明。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    commands = []
    for text, x, y in [
        ("ALPHA LEFT FIRST", 40, 740),
        ("RIGHT FIRST", 330, 740),
        ("LEFT LAST", 40, 715),
        ("RIGHT LAST", 330, 715),
        ("Field", 60, 650),
        ("Value", 320, 650),
        ("Name", 268 if narrow else 60, 615),
        ("BETA", 303 if narrow else 320, 615),
        ("Date:", 40, 540),
        ("2026-01-01", 320, 540),
    ]:
        commands.append(f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET")
    for y in (675, 635, 600):
        commands.append(f"40 {y} m 560 {y} l S")
    for x in (40, 300, 560):
        commands.append(f"{x} 600 m {x} 675 l S")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def test_native_pdf_public_entry_preserves_structure(tmp_path):
    from yuxi.knowledge.parser.unified import parse_pdf

    path = tmp_path / "layout.pdf"
    build_layout_pdf(path)
    result = parse_pdf(path, {"ocr_engine": "disable"})
    assert result.index("LEFT LAST") < result.index("RIGHT FIRST")
    assert "| Name | BETA |" in result
    assert "Date: 2026-01-01" in result


def test_rapid_pdf_uses_native_structure_without_loading_ocr(tmp_path, monkeypatch):
    path = tmp_path / "layout.pdf"
    build_layout_pdf(path)
    parser = RapidOCRParser()
    monkeypatch.setattr(parser, "_load_model", lambda: pytest.fail("文字页不需要加载 OCR"))
    result = parser.process_pdf(str(path))
    assert result.index("LEFT LAST") < result.index("RIGHT FIRST")
    assert "| Name | BETA |" in result


def test_scanned_pdf_cannot_succeed_with_ocr_disabled(tmp_path):
    from yuxi.knowledge.parser.unified import parse_pdf

    path = tmp_path / "scan.pdf"
    Image.new("RGB", (200, 200), "black").save(path, "PDF")
    with pytest.raises(ValueError, match="启用 OCR"):
        parse_pdf(path, {"ocr_engine": "disable"})


def box(text, x, y, width=180):
    """构造已知位置的样例行。"""
    return TextBox(text, x, y, x + width, y + 10)


def test_double_column_with_full_width_title():
    boxes = [
        box("TITLE", 40, 10, 520),
        box("RIGHT FIRST", 320, 40),
        box("LEFT FIRST", 40, 40),
        box("RIGHT LAST", 320, 70),
        box("LEFT LAST", 40, 70),
    ]
    assert render_layout(boxes, 600).split("\n\n") == ["TITLE", "LEFT FIRST", "LEFT LAST", "RIGHT FIRST", "RIGHT LAST"]


def test_off_center_columns_use_actual_gutter():
    boxes = [box("L1", 50, 20, 265), box("R1", 345, 20, 225), box("L2", 50, 50, 265), box("R2", 345, 50, 225)]
    assert render_layout(boxes, 600) == "L1\n\nL2\n\nR1\n\nR2"


def test_separate_tables_and_segmented_vertical_lines():
    boxes, horizontal, vertical = [], [], []
    for offset, value in [(0, "ALPHA"), (140, "BETA")]:
        boxes.extend(
            [
                box("Field", 60, 25 + offset, 45),
                box("Value", 320, 25 + offset, 45),
                box("Name", 60, 60 + offset, 45),
                box(value, 320, 60 + offset, 60),
            ]
        )
        horizontal.extend((40, y + offset, 560) for y in (15, 50, 85))
        vertical.extend(
            (x, top + offset, bottom + offset) for x in (40, 300, 560) for top, bottom in [(15, 50), (50, 85)]
        )
    result = render_layout(boxes, 600, horizontal, vertical)
    assert result.count("| Field | Value |") == 2
    assert "| Name | ALPHA |" in result and "| Name | BETA |" in result


def test_partial_column_is_not_silently_assigned_to_neighbor():
    boxes = [box("Name", 60, 25, 45), box("ALPHA", 320, 25, 45)]
    with pytest.raises(ValueError, match="合并单元格"):
        render_layout(boxes, 600, [(40, y, 560) for y in (15, 50, 85)], [(300, 15, 50)])


def test_cross_column_ocr_box_is_not_silently_assigned():
    with pytest.raises(ValueError, match="跨越列边界"):
        render_layout(
            [box("Name ALPHA", 60, 25, 400)],
            600,
            [(40, y, 560) for y in (15, 50, 85)],
            [(x, 15, 85) for x in (40, 300, 560)],
        )


def test_three_line_table_preserves_wrapped_description_and_body():
    boxes = [
        box("BODY FIRST", 30, 30),
        box("BODY LAST", 30, 80),
        box("Level", 330, 35, 35),
        box("Meaning", 410, 35, 100),
        box("A", 330, 55, 20),
        box("Strong evidence", 410, 55, 130),
        box("continued", 410, 70, 100),
        box("B", 330, 90, 20),
        box("Limited evidence", 410, 90, 130),
    ]
    result = render_layout(boxes, 600, [(320, 25, 560), (320, 48, 560), (320, 110, 560)])
    assert "| A | Strong evidence continued |" in result
    assert "| B | Limited evidence |" in result
    assert result.index("BODY LAST") < result.index("| Level")


def test_ruled_form_keeps_empty_values_and_independent_rows():
    boxes = [
        box("Field", 60, 25, 45),
        box("Value", 320, 25, 45),
        box("Name", 60, 60, 45),
        box("ALPHA", 320, 60, 60),
        box("Date", 60, 95, 45),
    ]
    horizontal = [(40, y, 560) for y in (15, 50, 85, 120)]
    vertical = [(x, 15, 120) for x in (40, 300, 560)]
    result = render_layout(boxes, 600, horizontal, vertical)
    assert "| Name | ALPHA |" in result
    assert "| Date |  |" in result
    assert result.count("ALPHA") == 1


def test_unruled_explicit_fields_are_not_read_as_columns():
    boxes = [
        box("Name:", 40, 20, 50),
        box("ALPHA", 320, 20, 70),
        box("Date:", 40, 50, 50),
        box("2026-01-01", 320, 50, 90),
    ]
    assert render_layout(boxes, 600) == "Name: ALPHA\n\nDate: 2026-01-01"


def test_scan_grid_detection_retains_table_cells():
    image = Image.new("RGB", (600, 200), "white")
    draw = ImageDraw.Draw(image)
    for y in (20, 60, 100):
        draw.line((40, y, 560, y), fill="black", width=2)
    for x in (40, 300, 560):
        draw.line((x, 20, x, 100), fill="black", width=2)
    horizontal, vertical = image_rules(image)
    boxes = [box("Field", 60, 30, 50), box("Value", 320, 30, 50), box("Name", 60, 70, 50), box("ALPHA", 320, 70, 60)]
    assert "| Name | ALPHA |" in render_layout(boxes, 600, horizontal, vertical)


def test_ocr_rejects_missing_coordinates_instead_of_flattening():
    parser = RapidOCRParser()
    with pytest.raises(ValueError, match="坐标"):
        parser._layout_result(SimpleNamespace(txts=["data"], boxes=None), Image.new("RGB", (100, 100), "white"))


def test_ocr_rejects_nonempty_page_without_recognized_text():
    with pytest.raises(ValueError, match="未识别"):
        RapidOCRParser()._layout_result(SimpleNamespace(txts=None), Image.new("RGB", (100, 100), "black"))


def test_ocr_coordinates_restore_column_order():
    image = Image.new("RGB", (600, 120), "white")
    result = SimpleNamespace(
        txts=["L1", "R1", "L2", "R2"],
        boxes=np.array(
            [
                [[30, 20], [220, 20], [220, 30], [30, 30]],
                [[330, 20], [520, 20], [520, 30], [330, 30]],
                [[30, 50], [220, 50], [220, 60], [30, 60]],
                [[330, 50], [520, 50], [520, 60], [330, 60]],
            ]
        ),
    )
    boxes, horizontal, vertical = RapidOCRParser()._layout_result(result, image)
    assert render_layout(boxes, 600, horizontal, vertical) == "L1\n\nL2\n\nR1\n\nR2"


def test_native_narrow_padding_respects_cell_boundary(tmp_path):
    from yuxi.knowledge.parser.unified import parse_pdf

    path = tmp_path / "narrow.pdf"
    build_layout_pdf(path, narrow=True)
    assert "| Name | BETA |" in parse_pdf(path, {"ocr_engine": "disable"})


def test_pdf_widget_value_is_not_lost_when_absent_from_content_stream(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
    from yuxi.knowledge.parser.pdf_layout import parse_local_pdf

    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("Patient name"),
            NameObject("/V"): TextStringObject("SYNTHETIC"),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in [50, 600, 250, 630]]),
        }
    )
    page[NameObject("/Annots")] = ArrayObject([writer._add_object(widget)])
    path = tmp_path / "widget.pdf"
    writer.write(path)
    assert parse_local_pdf(path) == "Patient name: SYNTHETIC"


def test_small_image_on_text_page_requires_ocr(tmp_path):
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
    from yuxi.knowledge.parser.pdf_layout import parse_local_pdf

    native, image_pdf = tmp_path / "native.pdf", tmp_path / "image.pdf"
    build_layout_pdf(native)
    Image.new("RGB", (100, 100), "black").save(image_pdf, "PDF")
    reader, image_reader = PdfReader(native), PdfReader(image_pdf)
    image_page = image_reader.pages[0]
    image_page.add_transformation(Transformation().scale(0.2).translate(400, 400))
    reader.pages[0].merge_page(image_page)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    widget = DictionaryObject(
        {
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/T"): TextStringObject("Stored field"),
            NameObject("/V"): TextStringObject("UNRENDERED"),
            NameObject("/Rect"): ArrayObject([NumberObject(v) for v in [50, 300, 250, 320]]),
        }
    )
    writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(widget)])
    path = tmp_path / "mixed.pdf"
    writer.write(path)
    with pytest.raises(ValueError, match="启用 OCR"):
        parse_local_pdf(path)
    result = parse_local_pdf(path, ocr_page=lambda *_: ([box("IMAGE AND TEXT", 40, 20)], [], []))
    assert result == "IMAGE AND TEXT\n\nStored field: UNRENDERED"


@pytest.mark.parametrize("coordinates", [np.zeros((1, 3, 2)), np.full((1, 4, 2), np.nan)])
def test_invalid_ocr_geometry_is_rejected(coordinates):
    with pytest.raises(ValueError, match="坐标无效"):
        RapidOCRParser()._layout_result(SimpleNamespace(txts=["text"], boxes=coordinates), Image.new("RGB", (100, 100)))


def test_three_column_three_line_table_keeps_entire_records():
    boxes = [
        box(text, x, y, 50)
        for y, values in [(25, ["Drug", "Dose", "Route"]), (55, ["A", "10mg", "oral"]), (80, ["B", "20mg", "IV"])]
        for x, text in zip([50, 230, 430], values)
    ]
    result = render_layout(boxes, 600, [(40, y, 560) for y in [15, 45, 100]])
    assert "| Drug | Dose | Route |" in result
    assert "| A | 10mg | oral |" in result
    assert "| B | 20mg | IV |" in result


def test_two_unruled_three_line_tables_are_separate():
    boxes, horizontal = [], []
    for offset, value in [(0, "ALPHA"), (140, "BETA")]:
        boxes.extend(
            [
                box("Field", 60, 25 + offset, 50),
                box("Value", 320, 25 + offset, 50),
                box("Name", 60, 60 + offset, 50),
                box(value, 320, 60 + offset, 50),
            ]
        )
        horizontal.extend((40, y + offset, 560) for y in [15, 45, 90])
    result = render_layout(boxes, 600, horizontal)
    assert result.count("| Field | Value |") == 2
    assert "| Name | ALPHA |" in result and "| Name | BETA |" in result


def test_unresolved_ruled_table_does_not_become_plain_text():
    with pytest.raises(ValueError, match="无法确定列关系"):
        render_layout([box("unclear merged row", 50, 25, 400)], 600, [(40, y, 560) for y in [15, 45, 90]])


def test_scanned_pdf_uses_real_renderer_with_coordinate_ocr(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    image = Image.new("RGB", (200, 200), "white")
    ImageDraw.Draw(image).text((10, 20), "FIELD", fill="black")
    image.save(path, "PDF")
    parser = RapidOCRParser()
    monkeypatch.setattr(parser, "_load_model", lambda: None)
    parser.ocr = lambda _: SimpleNamespace(txts=["FIELD"], boxes=np.array([[[10, 20], [100, 20], [100, 40], [10, 40]]]))
    assert parser.process_pdf(str(path)) == "FIELD"


def test_cyclic_form_parent_is_rejected():
    from pdfminer.psparser import PSLiteral
    from yuxi.knowledge.parser.pdf_layout import form_fields

    parent = {}
    parent["Parent"] = parent
    page = SimpleNamespace(annots=[{"data": {"Subtype": PSLiteral("Widget"), "Parent": parent}}])
    with pytest.raises(ValueError, match="父级循环"):
        form_fields(page)


def build_narrow_gutter_pdf(path):
    """构造窄栏间距、左下标题与右上续文的独立阅读顺序样例。"""
    from pypdf import PdfReader
    from pypdf.generic import DecodedStreamObject, NameObject
    from pypdf import PdfWriter

    build_layout_pdf(path)
    writer = PdfWriter(clone_from=PdfReader(path))
    stream = DecodedStreamObject()
    commands = []
    # Courier 每字符 6 点；36字符由 x=72 延伸到288，右栏由298开始，间距仅10点。
    lines = [
        ("A FULL WIDTH HEADING" * 4, 40, 770),
        ("LEFT SECTION FOUR FIRST", 72, 730),
        ("RIGHT SECTION FIVE CONTINUATION", 298, 730),
        ("LEFT SECTION FOUR MIDDLE", 72, 710),
        ("RIGHT SECTION FIVE TRIAL ONE", 298, 710),
        ("L" * 36, 72, 690),
        ("RIGHT SHORT", 298, 690),
        ("LEFT SECTION FOUR END", 72, 670),
        ("RIGHT SECTION FIVE TRIAL TWO", 298, 670),
        ("5 SECTION FIVE TITLE", 72, 650),
        ("RIGHT SECTION FIVE CONCLUSION", 298, 650),
        ("LEFT SECTION FIVE START", 72, 630),
    ]
    page = writer.pages[0]
    page["/Resources"]["/Font"]["/F1"][NameObject("/BaseFont")] = NameObject("/Courier")
    for text, x, y in lines:
        commands.append(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET")
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def test_native_narrow_gutter_does_not_move_continuation_before_section(tmp_path):
    """短于旧阈值的栏间距不能制造跨栏块，把下一节续文移到标题前。"""
    from yuxi.knowledge.parser.pdf_layout import parse_local_pdf

    path = tmp_path / "narrow-gutter.pdf"
    build_narrow_gutter_pdf(path)
    result = parse_local_pdf(path)
    expected = [
        "A FULL WIDTH HEADING" * 4,
        "LEFT SECTION FOUR FIRST",
        "LEFT SECTION FOUR MIDDLE",
        "L" * 36,
        "LEFT SECTION FOUR END",
        "5 SECTION FIVE TITLE",
        "LEFT SECTION FIVE START",
        "RIGHT SECTION FIVE CONTINUATION",
        "RIGHT SECTION FIVE TRIAL ONE",
        "RIGHT SHORT",
        "RIGHT SECTION FIVE TRIAL TWO",
        "RIGHT SECTION FIVE CONCLUSION",
    ]
    assert result.split("\n\n") == expected


def test_varying_indents_use_shared_gutter_instead_of_gap_midpoints():
    """短行和右栏缩进变化时，真实共同栏缝不一定包含任何空隙中点。"""
    from yuxi.knowledge.parser.pdf_layout import reading_order

    boxes = []
    for i, (left_end, right_start) in enumerate(
        [(220, 305), (220, 410), (260, 305), (295, 330), (285, 330), (220, 305)]
    ):
        boxes.extend(
            [box(f"L{i}", 40, 20 + 20 * i, left_end - 40), box(f"R{i}", right_start, 20 + 20 * i, 560 - right_start)]
        )
    assert [b.text for b in reading_order(boxes, 600)] == [f"L{i}" for i in range(6)] + [f"R{i}" for i in range(6)]
