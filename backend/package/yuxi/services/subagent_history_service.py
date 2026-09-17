"""退役子运行的只读历史投影，不提供创建或执行能力。"""

from yuxi.storage.postgres.models_business import AgentRun
from yuxi.utils.datetime_utils import format_utc_datetime


def subagent_run_urls(run_id: str) -> dict[str, str]:
    """生成子智能体 run 对外暴露的事件流和结果查询 URL。"""
    return {
        "events_url": f"/api/agent/runs/{run_id}/events",
        "result_url": f"/api/agent/runs/{run_id}/result",
    }


def serialize_subagent_run_state(run: AgentRun) -> dict:
    """序列化给父智能体状态使用的子智能体 run 摘要。

    任务描述不在此冗余存储：其唯一来源是父对话里 `task` 工具调用的入参，
    前端面板按 tool_call_id 回填展示。
    """
    payload = run.input_payload
    if not isinstance(payload, dict):
        raise ValueError("subagent run 缺少 input_payload")
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        raise ValueError("subagent run 缺少 runtime")
    tool_call_id = str(runtime.get("tool_call_id") or "").strip()
    if not tool_call_id:
        raise ValueError("subagent run 缺少 tool_call_id")

    state = {
        "id": tool_call_id,
        "run_id": run.id,
        "subagent_slug": run.agent_slug,
        "subagent_name": runtime.get("subagent_name"),
        "child_thread_id": run.conversation_thread_id,
        "status": run.status,
        "created_at": format_utc_datetime(run.created_at),
        "completed_at": format_utc_datetime(run.finished_at),
        "error": run.error_message,
        **subagent_run_urls(run.id),
    }
    return {key: value for key, value in state.items() if value is not None}
