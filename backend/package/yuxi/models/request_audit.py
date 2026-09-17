"""在异步模型请求发送前读取正文，按当前调用上下文交给审计 Owner。"""

import json
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import httpx

ModelRequestRecorder = Callable[[str, dict], Awaitable[None]]
model_request_recorder: ContextVar[tuple[str, ModelRequestRecorder] | None] = ContextVar(
    "model_request_recorder", default=None
)


async def audit_model_request(request: httpx.Request) -> None:
    """仅采集模型 JSON 正文，不读取 URL、认证和其他请求头。"""
    binding = model_request_recorder.get()
    if binding is None or request.method != "POST":
        return
    if not request.url.path.rstrip("/").endswith(("/chat/completions", "/responses")):
        return
    model_run_id, record = binding
    body = json.loads(await request.aread())
    await record(model_run_id, body)
