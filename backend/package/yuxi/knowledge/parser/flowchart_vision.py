"""用已配置的视觉聊天模型转写 MinerU 标出的流程图。"""

import asyncio
import base64
import json

import pymupdf
from langchain_core.messages import HumanMessage

from yuxi.knowledge.structure import PLACEHOLDER_FIGURE_BLOCK
from yuxi.models.chat import load_chat_model

PROMPT_VERSION = 2
PROMPT = """只分析第一张图片中的图示或流程图；如有第二张整页图，只用它核对标题、脚注和跨页引用。
逐字保留图中的原文、缩写、分期、时间、推荐类别和脚注，不翻译、不补充医学常识。
每条可见箭头单独记录起点、终点、方向及箭头旁的条件；不能凭左右位置猜测连接。
明确可见的 OR/AND 或分支关系才填写 relation；看不清的文字或连线写入 uncertainties，不猜测。
最终回复的第一个字符必须是 {、最后一个字符必须是 }；只返回一个 JSON 对象，不要解释或 Markdown 代码块：
{
  "is_flowchart": true,
  "title": "图中原文标题或空串",
  "nodes": [{"id": "n1", "kind": "finding", "text": "第一个节点的可见原文", "bbox": null}, {"id": "n2", "kind": "treatment", "text": "第二个节点的可见原文", "bbox": null}],
  "edges": [{"from": "n1", "to": "n2", "condition": "箭头旁原文或空串", "relation": "sequence"}],
  "footnotes": [{"marker": "a", "text": "与图相关的脚注原文"}],
  "uncertainties": ["无法确认的文字或箭头"]
}
示例文字只是字段说明，不能复制进结果；所有字段均须输出，无法识别的文字写入 uncertainties。kind 只能取 finding、workup、decision、result、treatment、reference、other；relation 只能取 sequence、branch、alternative、parallel，普通箭头用 sequence。
bbox 能定位时填写第一张图中 0 到 1000 的归一化坐标 [左, 上, 右, 下]；看不清时填 null。
如果第一张图不是流程图，返回 is_flowchart=false，其他列表为空。"""

NODE_KINDS = {"finding", "workup", "decision", "result", "treatment", "reference", "other"}
EDGE_RELATIONS = {"sequence", "branch", "alternative", "parallel"}


def _render_images(pdf_bytes: bytes, page_number: int, bbox: list[float]) -> list[bytes]:
    """渲染图示区域与整页上下文，保留原 PDF 页面作为唯一视觉来源。"""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if not 1 <= page_number <= len(pdf):
            raise ValueError("图示页码超出原 PDF")
        page = pdf[page_number - 1]
        if len(bbox) != 4:
            raise ValueError("图示坐标无效")
        area = pymupdf.Rect(*bbox) & page.rect
        if area.is_empty or area.width < 4 or area.height < 4:
            raise ValueError("图示区域为空")
        crop = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=area, alpha=False).tobytes("png")
        if area.get_area() >= page.rect.get_area() * 0.98:
            return [crop]
        full_page = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).tobytes("png")
        return [crop, full_page]


def _response_text(response) -> str:
    """从聊天模型响应中取出 JSON 文本。"""
    content = response.content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        content = "".join(parts)
    if not isinstance(content, str):
        raise ValueError("视觉模型没有返回文本")
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return content


def _graph_from_text(content: str) -> dict | None:
    """校验模型 JSON 的节点、连线和长度，拒绝无法核对的结构。"""
    content = content.strip()
    if content.startswith("<think>") and "</think>" in content:
        content = content.rsplit("</think>", 1)[-1].strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(content[start : end + 1])
    if not isinstance(data, dict) or type(data.get("is_flowchart")) is not bool:
        raise ValueError("视觉模型未返回流程图判定")
    if not data["is_flowchart"]:
        return None
    nodes, edges = data.get("nodes"), data.get("edges")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 80:
        raise ValueError("流程图节点数量无效")
    if not isinstance(edges, list) or not 1 <= len(edges) <= 160:
        raise ValueError("流程图连线数量无效")

    clean_nodes, ids, invalid_locations = [], set(), []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("流程图节点无效")
        node_id, kind, value = node.get("id"), node.get("kind"), node.get("text")
        if (
            not isinstance(node_id, str)
            or not 1 <= len(node_id) <= 64
            or node_id in ids
            or not isinstance(kind, str)
            or kind not in NODE_KINDS
            or not isinstance(value, str)
            or not 1 <= len(value.strip()) <= 1200
        ):
            raise ValueError("流程图节点标识或文字无效")
        bbox = node.get("bbox")
        if bbox is not None and (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(v) not in {int, float} or not 0 <= v <= 1000 for v in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            bbox = None
            invalid_locations.append(node_id)
        ids.add(node_id)
        clean_nodes.append({"id": node_id, "kind": kind, "text": value.strip(), "bbox": bbox})

    clean_edges = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError("流程图连线无效")
        source, target = edge.get("from"), edge.get("to")
        condition = edge.get("condition")
        relation = edge.get("relation")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in ids
            or target not in ids
            or source == target
            or not isinstance(condition, str)
            or len(condition) > 300
            or not isinstance(relation, str)
            or relation not in EDGE_RELATIONS
        ):
            raise ValueError("流程图连线端点或条件无效")
        clean_edges.append({"from": source, "to": target, "condition": condition.strip(), "relation": relation})

    footnotes = data.get("footnotes")
    uncertainties = data.get("uncertainties")
    title = data.get("title")
    if (
        not isinstance(title, str)
        or len(title) > 300
        or not isinstance(footnotes, list)
        or len(footnotes) > 40
        or not isinstance(uncertainties, list)
        or len(uncertainties) > 30
    ):
        raise ValueError("流程图标题或附注无效")
    clean_footnotes = []
    for note in footnotes:
        if (
            not isinstance(note, dict)
            or not isinstance(note.get("marker"), str)
            or not isinstance(note.get("text"), str)
        ):
            raise ValueError("流程图脚注无效")
        if len(note["marker"]) > 20 or len(note["text"]) > 1000:
            raise ValueError("流程图脚注过长")
        clean_footnotes.append({"marker": note["marker"].strip(), "text": note["text"].strip()})
    if any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in uncertainties):
        raise ValueError("流程图不确定项无效")
    if invalid_locations:
        uncertainties.append(f"节点 {', '.join(invalid_locations)} 的定位坐标无效，已清空，请对照原图核对")
    return {
        "title": title.strip(),
        "nodes": clean_nodes,
        "edges": clean_edges,
        "footnotes": clean_footnotes,
        "uncertainties": [item.strip() for item in uncertainties],
    }


def render_flowchart_text(graph: dict) -> str:
    """从已校验的图结构确定性生成可检索的条件与去向文本。"""
    nodes = {node["id"]: node["text"] for node in graph["nodes"]}
    lines = [f"流程图：{graph['title']}" if graph["title"] else "流程图"]
    lines.extend(f"节点 {node['id']}（{node['kind']}）：{node['text']}" for node in graph["nodes"])
    for edge in graph["edges"]:
        condition = f"；条件：{edge['condition']}" if edge["condition"] else ""
        relation = f"；关系：{edge['relation']}" if edge["relation"] != "sequence" else ""
        lines.append(f"路径：{nodes[edge['from']]}{condition}{relation} → {nodes[edge['to']]}")
    lines.extend(f"脚注 {note['marker']}：{note['text']}" for note in graph["footnotes"])
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 50000:
        raise ValueError("流程图文本超过结构块上限")
    return text


async def enrich_mineru_flowcharts(structure: dict, pdf_bytes: bytes, model_spec: str) -> None:
    """只替换 MinerU 图示占位，其他文块维持原解析结果。"""
    model = load_chat_model(model_spec)
    pages = {page["page"]: page for page in structure["pages"]}
    consecutive_errors = 0
    for block in structure["blocks"]:
        if block["kind"] != "relationship" or block.get("source_label") != "image":
            continue
        page = pages[block["page"]]
        if consecutive_errors >= 2:
            block["text"] = PLACEHOLDER_FIGURE_BLOCK
            page["issues"].append("视觉模型连续两次调用未生成可用结构，已停止后续调用以避免继续消耗")
            continue

        try:
            images = await asyncio.to_thread(_render_images, pdf_bytes, block["page"], block["bbox"])
        except Exception as exc:
            block["text"] = PLACEHOLDER_FIGURE_BLOCK
            page["issues"].append(f"图示区域渲染失败（{type(exc).__name__}），请对照原页补录")
            continue

        content = [{"type": "text", "text": PROMPT}]
        content.extend(
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()}}
            for data in images
        )
        block["vision_model_spec"] = model_spec
        block["vision_prompt_version"] = PROMPT_VERSION
        try:
            response = await asyncio.wait_for(model.ainvoke([HumanMessage(content=content)]), timeout=180)
        except Exception as exc:
            consecutive_errors += 1
            block["text"] = PLACEHOLDER_FIGURE_BLOCK
            page["issues"].append(f"视觉模型调用失败（{type(exc).__name__}），请对照原页补录图示")
            continue

        try:
            raw_response = _response_text(response)
        except ValueError:
            raw_response = repr(response.content)
        block["vision_raw_response"] = raw_response[:100000]
        block["vision_response_truncated"] = len(raw_response) > 100000
        metadata = response.response_metadata or {}
        block["vision_finish_reason"] = metadata.get("finish_reason")
        block["vision_usage"] = response.usage_metadata or {}

        try:
            graph = _graph_from_text(raw_response)
            text = render_flowchart_text(graph) if graph is not None else None
        except Exception as exc:
            consecutive_errors += 1
            block["text"] = PLACEHOLDER_FIGURE_BLOCK
            page["issues"].append(f"视觉模型解析未完成（{type(exc).__name__}），请对照原页补录图示")
            continue

        consecutive_errors = 0
        if graph is None:
            block["text"] = PLACEHOLDER_FIGURE_BLOCK
            page["issues"].append("视觉模型未识别为流程图，请对照原页确认图示内容或排除")
            continue
        block["text"] = text
        block["vision_graph"] = graph
        page["issues"].append("视觉模型已转写流程图，请核对节点文字、条件、箭头方向和脚注")
        page["issues"].extend(f"视觉模型待核对：{item}" for item in graph["uncertainties"])
