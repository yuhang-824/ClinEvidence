"""诊疗 Agent 的四个只读患者工具:上下文、病历检索、知识检索、证据回读。

工具不向模型暴露 patient_id/snapshot_id 参数;作用域由服务端从 runtime
上下文(thread_id/uid)与 Run 绑定的快照解析。所有工具只读。
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, Field

from yuxi.agents.toolkits.registry import tool


def _runtime_identity(runtime: ToolRuntime | None) -> tuple[str, str] | None:
    """从 runtime 上下文读取 thread_id 与 uid;缺失即拒绝,不做猜测。"""
    context = getattr(runtime, "context", None) if runtime else None
    if context is None:
        return None
    thread_id = getattr(context, "runtime_scope_id", None) or getattr(context, "thread_id", None)
    uid = getattr(context, "uid", None)
    if not thread_id or not uid:
        return None
    return str(thread_id), str(uid)


async def _run_with_pg_session(func, runtime: ToolRuntime | None, *args, **kwargs):
    """在独立 PG 会话中执行服务函数;工具层不持有长事务。"""
    from yuxi.storage.postgres.manager import pg_manager

    identity = _runtime_identity(runtime)
    if identity is None:
        return "无法获取当前会话上下文,缺少 thread_id 或 uid"
    thread_id, uid = identity
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        return await func(db, thread_id=thread_id, uid=uid, **kwargs)


def _dumps(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


class ReadPatientContextInput(BaseModel):
    """read_patient_context 无业务参数;字段仅为满足 runtime 注入机制。"""

    dummy: str = Field("", description="未使用")


class SearchPatientRecordsInput(BaseModel):
    """病历检索参数;不包含任何患者或快照标识字段。"""

    query_text: str = Field(description="检索问题文本")
    top_k: int = Field(8, ge=1, le=20)
    document_types: list[str] | None = Field(None, description="可选文书类型过滤")


class SearchMedicalKnowledgeInput(BaseModel):
    """医学知识检索参数;复用通用知识库权限。"""

    kb_id: str = Field(description="知识库 ID")
    query_text: str = Field(description="检索问题文本")
    file_name: str | None = Field(None, description="可选文件名过滤")


class ReadEvidenceInput(BaseModel):
    """证据回读参数;服务端校验证据归属当前快照。"""

    chunk_id: str = Field(description="患者证据文块 ID")


@tool(
    category="clinical",
    tags=["患者"],
    display_name="患者上下文",
    args_schema=ReadPatientContextInput,
)
async def read_patient_context(dummy: str, runtime: ToolRuntime = None) -> str:
    """返回当前会话患者的脱敏摘要、就诊列表与本次快照信息。"""
    from yuxi.services.clinical_retrieval_service import get_patient_context_view

    return _dumps(await _run_with_pg_session(get_patient_context_view, runtime))


@tool(
    category="clinical",
    tags=["患者"],
    display_name="病历检索",
    args_schema=SearchPatientRecordsInput,
)
async def search_patient_records(
    query_text: str,
    top_k: int = 8,
    document_types: list[str] | None = None,
    runtime: ToolRuntime = None,
) -> str:
    """在当前患者已发布快照内检索病历文块;仅限当前患者,不可检索他人资料。"""
    from yuxi.services.clinical_retrieval_service import search_patient_records_view

    async def _impl(db, *, thread_id: str, uid: str) -> Any:
        return await search_patient_records_view(
            db,
            thread_id=thread_id,
            uid=uid,
            query_text=query_text,
            top_k=top_k,
            document_types=document_types,
        )

    return _dumps(await _run_with_pg_session(_impl, runtime))


@tool(
    category="clinical",
    tags=["知识"],
    display_name="医学知识检索",
    args_schema=SearchMedicalKnowledgeInput,
)
async def search_medical_knowledge(
    kb_id: str, query_text: str, file_name: str | None = None, runtime: ToolRuntime = None
) -> Any:
    """在授权的医学知识库中检索指南与共识;知识证据与患者事实分别标注。"""
    from yuxi.agents.toolkits.kbs.tools import (
        _find_query_target,
        _get_knowledge_base,
        _resolve_visible_knowledge_bases_for_query,
    )

    if not kb_id or not query_text:
        return "请提供 kb_id 与查询内容"
    visible_kbs = await _resolve_visible_knowledge_bases_for_query(runtime)
    target_kb_id, target_error = _find_query_target(kb_id=kb_id, visible_kbs=visible_kbs)
    if target_error:
        return target_error
    try:
        kwargs = {"file_name": file_name} if file_name else {}
        return await _get_knowledge_base().retrieve(target_kb_id, query_text, **kwargs)
    except Exception as exc:
        return f"检索失败: {exc}"


@tool(
    category="clinical",
    tags=["患者"],
    display_name="证据原文回读",
    args_schema=ReadEvidenceInput,
)
async def read_evidence_excerpt(chunk_id: str, runtime: ToolRuntime = None) -> str:
    """按证据 ID 回读患者原文窗口;服务端校验证据属于当前 Run 快照。"""
    from yuxi.services.clinical_retrieval_service import read_patient_evidence_view

    async def _impl(db, *, thread_id: str, uid: str) -> Any:
        return await read_patient_evidence_view(db, thread_id=thread_id, uid=uid, chunk_id=chunk_id)

    return _dumps(await _run_with_pg_session(_impl, runtime))


def get_clinical_tools() -> list:
    """返回诊疗 Agent 首期注册的四个只读工具。"""
    return [read_patient_context, search_patient_records, search_medical_knowledge, read_evidence_excerpt]
