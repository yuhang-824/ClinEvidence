"""确定性的文档格式清洗与人工核验提示。"""

import re
from collections import Counter
from statistics import median

PAGE_BREAK = "\n\n<!-- clinevidence:page-break -->\n\n"


def clean_document(raw: str) -> tuple[str, dict]:
    """仅清理格式噪声，保留医学字符和所有表格单元格。"""
    pages = raw.replace("\r\n", "\n").replace("\r", "\n").split(PAGE_BREAK)
    margins = Counter()
    for page in pages:
        lines = [line.strip() for line in page.splitlines() if line.strip()]
        margins.update(set(lines))
    repeated = {line for line, count in margins.items() if len(pages) >= 3 and count >= max(3, len(pages) * 0.6)}
    changes, warnings, cleaned_pages = [], [], []
    for page_number, page in enumerate(pages, 1):
        lines = page.splitlines()
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        edges = set(nonempty[:4] + nonempty[-4:])
        output = []
        in_code = False
        for i, original in enumerate(lines):
            if original.lstrip().startswith(("```", "~~~")):
                in_code = not in_code
                output.append(original)
                continue
            if in_code:
                output.append(original)
                continue
            line = original.rstrip()
            stripped = line.strip()
            # 栏排序可能把页眉放到右栏前；完整刊名+年月卷期在多页重复仍可确定其身份。
            journal_header = re.fullmatch(r"(?:中国|中华).{0,60}杂志\s*\d{4}年.*第\s*\d+卷.*第\s*\d+期", stripped)
            running_header = stripped in repeated and journal_header
            page_number_line = re.fullmatch(
                r"(?:第\s*\d+\s*页(?:\s*[共/]\s*\d+\s*页)?|Page\s+\d+(?:\s+of\s+\d+)?|[·•—–-]\s*\d+\s*[·•—–-])",
                stripped,
                re.I,
            )
            protected = "|" in line or stripped.startswith(("#", "- ", "* ", ">", "```"))
            decorated_number = re.fullmatch(r"[·•]\s*\d+\s*[·•]", stripped)
            if (
                len(pages) > 1
                and not protected
                and (running_header or decorated_number or (i in edges and page_number_line))
            ):
                changes.append({"page": page_number, "line": i + 1, "kind": "margin", "before": original, "after": ""})
                continue
            if original != line:
                changes.append(
                    {"page": page_number, "line": i + 1, "kind": "trailing_space", "before": original, "after": line}
                )
            if not line and output and not output[-1]:
                changes.append(
                    {"page": page_number, "line": i + 1, "kind": "blank_line", "before": original, "after": ""}
                )
                continue
            # 中文硬换行只在普通连续文字之间合并，数字、单位、字段和表格不参与。
            if (
                output
                and re.search(r"[\u4e00-\u9fff]$", output[-1])
                and re.match(r"^[\u4e00-\u9fff]", line)
                and not protected
            ):
                previous = output[-1]
                if re.fullmatch(r"[\u4e00-\u9fff，、（）]+", previous) and not re.search(
                    r"[\dA-Za-z:：]|^(推荐|共识|结论|建议|不建议|禁忌|患者|姓名|诊断)", line
                ):
                    output[-1] += line
                    changes.append(
                        {
                            "page": page_number,
                            "line": i + 1,
                            "kind": "join_line",
                            "before": previous + "\n" + line,
                            "after": output[-1],
                        }
                    )
                    continue
            output.append(line)
        content = "\n".join(output).strip()
        if len(content) < 40:
            warnings.append(
                {
                    "page": page_number,
                    "code": "sparse_page",
                    "message": "本页文字较少或为空，请检查漏识别、图片及空白页。",
                }
            )
        if re.search(r"\ufffd|\(cid:\d+\)", content):
            warnings.append(
                {"page": page_number, "code": "unrecognized", "message": "存在无法识别的字符，请对照原文修订。"}
            )
        table_width = None
        for line in output:
            if line.strip().startswith("|"):
                width = len(re.split(r"(?<!\\)\|", line.strip()))
                if table_width is not None and width != table_width:
                    warnings.append(
                        {
                            "page": page_number,
                            "code": "table_columns",
                            "message": "表格行的列数不一致，请核验表头及字段关系。",
                        }
                    )
                    break
                table_width = width
            else:
                table_width = None
        cleaned_pages.append(content)
    typical_length = median(len(page) for page in cleaned_pages)
    if len(pages) >= 3 and typical_length >= 200:
        for page_number, page in enumerate(cleaned_pages, 1):
            if 40 <= len(page) < typical_length * 0.45:
                warnings.append(
                    {
                        "page": page_number,
                        "code": "page_text_drop",
                        "message": "本页文字量显著少于其他页，可能漏栏或识别不全，也可能是图表页；请核对原 PDF。",
                    }
                )
    if len(pages) == 1:
        warnings.append(
            {
                "page": None,
                "code": "no_page_boundaries",
                "message": "未取得逐页边界，未自动删除重复页眉页脚；请核对 PDF 总页数和左右栏完整性。",
            }
        )
    return "\n\n".join(cleaned_pages), {
        "version": 1,
        "page_count": len(pages),
        "changes": changes,
        "warnings": warnings,
    }
