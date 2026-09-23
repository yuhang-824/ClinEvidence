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
    """获取当前会话绑定的患者上下文。

    适用场景:
    1. 回答任何患者相关问题前,先获取患者概况与可用资料范围
    2. 医生询问"这位患者的基本情况""现在有哪些资料"
    3. 需要确认本次回答所依据的快照时点

    返回结果:
    patient 为脱敏摘要(编号/状态/当前快照),encounters 为就诊列表;
    患者尚无已发布快照时会明确提示,此时病历检索不可用。

    使用规范:
    1. 无参数;作用域由服务端从当前会话解析,不接受患者标识
    2. 每次会话首轮建议调用一次,后续仅在怀疑资料变化时重查
    """
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
    """在当前患者的已发布病历快照内做向量+BM25 混合检索。

    适用场景:
    1. 查找患者的诊断、检查、病理、治疗经过等具体事实
    2. 需要为回答提供患者原文证据(返回 chunk_id 供引用与回读)
    3. 按文书类型缩小范围,如只看病理报告

    返回结果:
    命中文块列表,含 content(原文内容)、document_name(所属病历文件)、
    document_type(文书类型)、page_number(页码)、score(向量相似度,仅按
    向量路命中的文块携带;仅 BM25 词面命中的文块无 score,排序由融合分
    rrf_score 决定)、chunk_id(证据 ID,可用于 read_evidence_excerpt 回读)、
    snapshot_id(证据所属快照)。

    使用规范:
    1. 只能检索当前会话患者;问题中出现其他患者编号不会改变作用域
    2. 检索词用临床术语,避免整句疑问
    3. 结果为空时如实说明未找到,不要编造患者事实
    4. 通用医学知识请改用 query_kb,不要用本工具检索指南原文
    """
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
    tags=["患者"],
    display_name="证据原文回读",
    args_schema=ReadEvidenceInput,
)
async def read_evidence_excerpt(chunk_id: str, runtime: ToolRuntime = None) -> str:
    """按证据 ID 回读患者病历原文窗口。

    适用场景:
    1. 多轮对话中上下文被压缩后,重新载入关键患者证据原文
    2. 回答前核对某个引用对应的原文内容,避免引用漂移
    3. 检索结果中某个 chunk 的内容片段不足以判断时查看完整文块

    返回结果:
    证据文块的完整原文、所属文书类型、页码、字符区间与快照归属;
    证据不属于当前患者快照时返回 404,不会返回其他患者内容。

    使用规范:
    1. chunk_id 必须来自 search_patient_records 的返回或已保存的引用
    2. 不要虚构 chunk_id;伪造 ID 会被拒绝并应向医生说明
    """
    from yuxi.services.clinical_retrieval_service import read_patient_evidence_view

    async def _impl(db, *, thread_id: str, uid: str) -> Any:
        return await read_patient_evidence_view(db, thread_id=thread_id, uid=uid, chunk_id=chunk_id)

    return _dumps(await _run_with_pg_session(_impl, runtime))


def get_clinical_tools() -> list:
    """返回诊疗 Agent 的患者域只读工具;医学知识检索复用原生 query_kb,不重复提供。"""
    return [read_patient_context, search_patient_records, read_evidence_excerpt]
