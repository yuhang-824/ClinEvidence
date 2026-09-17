"""以实际 SDK 请求验证输入采集、并发隔离和失败保留。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import SystemMessage, HumanMessage

from yuxi.models.chat import ChatCompletionsAdapter
from yuxi.models.request_audit import model_request_recorder
from yuxi.services.model_message_audit_service import ModelMessageAuditCollector


class MemoryCollector(ModelMessageAuditCollector):
    """仅替换持久化，保留真实 callback 与 SDK 采集路径。"""

    def __init__(self):
        super().__init__(run_id="run", request_id="request", thread_id="thread", worker_id="worker")
        self.inputs = []

    async def record_input(self, model_run_id, body):
        """复制发送前快照，供独立 transport oracle 比较。"""
        self.inputs.append((model_run_id, json.loads(json.dumps(body))))

    async def on_llm_error(self, error, *, run_id, **kwargs):
        """协议单测不连接数据库。"""
        await self.on_llm_end(None, run_id=run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_wire_inputs_match_sdk_body_and_parallel_model_ids(stream):
    collector = MemoryCollector()
    received = []

    async def respond(request):
        """传输层独立读取最终请求体，且必须已经完成采集。"""
        body = json.loads(request.content)
        assert body in [item[1] for item in collector.inputs]
        assert request.headers["authorization"] == "Bearer never-record-this-key"
        received.append(body)
        await asyncio.sleep(0)
        base = {"id": "test", "created": 1, "model": "stub"}
        if stream:
            chunk = {
                **base,
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            }
            return httpx.Response(
                200,
                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                **base,
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatCompletionsAdapter(
            model="stub",
            api_key="never-record-this-key",
            http_async_client=client,
            temperature=0.3,
            extra_body={"top_k": 17},
            stream_usage=True,
        ).bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "本地指南检索",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
        )
        ids = [uuid4(), uuid4()]

        async def invoke(index):
            """交错发送两个不同输入，检查每个 ID 对应自身消息。"""
            messages = [SystemMessage(content="完整系统提示"), HumanMessage(content=f"case-{index}")]
            config = {"callbacks": [collector], "run_id": ids[index]}
            if stream:
                async for _ in model.astream(messages, config):
                    pass
            else:
                await model.ainvoke(messages, config)
            assert model_request_recorder.get() is None

        await asyncio.gather(invoke(0), invoke(1))
    assert len(collector.inputs) == len(received) == 2
    for call_id, body in collector.inputs:
        index = ids.index(next(item for item in ids if str(item) == call_id))
        assert body["messages"] == [
            {"role": "system", "content": "完整系统提示"},
            {"role": "user", "content": f"case-{index}"},
        ]
        assert body["top_k"] == 17 and body["temperature"] == 0.3
        assert body["tools"][0]["function"]["name"] == "search"
        assert "never-record-this-key" not in json.dumps(body)


@pytest.mark.asyncio
async def test_failed_request_and_sdk_retry_retain_same_input_id():
    collector = MemoryCollector()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, json={"error": "test"}))
    ) as client:
        model = ChatCompletionsAdapter(model="stub", api_key="test", http_async_client=client, max_retries=1)
        with pytest.raises(Exception, match="500"):
            await model.ainvoke("failure", {"callbacks": [collector]})
    assert len(collector.inputs) == 2
    assert collector.inputs[0] == collector.inputs[1]
    assert model_request_recorder.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_close_and_cancel_release_audit_context(cancel):
    """真实 SDK 流提前关闭或取消后，不向下一次未审计调用串入旧输入。"""
    collector = MemoryCollector()
    started = asyncio.Event()

    class StreamBody(httpx.AsyncByteStream):
        """首片之后一直等待，提供确定性的取消窗口。"""

        async def __aiter__(self):
            yield b'data: {"id":"test","model":"stub","choices":[{"index":0,"delta":{"content":"OK"}}]}\n\n'
            await asyncio.Event().wait()

    def respond(request):
        """分别提供流式和普通响应。"""
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, stream=StreamBody(), headers={"content-type": "text/event-stream"})
        return httpx.Response(
            200,
            json={
                "id": "test",
                "model": "stub",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatCompletionsAdapter(model="stub", api_key="test", http_async_client=client)

        async def consume():
            """在持有流的同一任务检查清理结果。"""
            stream = model.astream("first", {"callbacks": [collector]})
            try:
                await anext(stream)
                started.set()
                if cancel:
                    await anext(stream)
            finally:
                await stream.aclose()
                assert model_request_recorder.get() is None
                await model.ainvoke("untraced next call")

        if cancel:
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await consume()
    assert len(collector.inputs) == 1


@pytest.mark.asyncio
async def test_audit_write_failure_prevents_transport_send():
    """持久化失败时 transport 永远不能收到模型请求。"""

    class FailingCollector(MemoryCollector):
        """模拟审计存储不可写。"""

        async def record_input(self, model_run_id, body):
            """在真实 HTTP hook 处制造失败。"""
            raise RuntimeError("audit write failed")

    sent = []

    def respond(request):
        """独立传输观察点。"""
        sent.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ChatCompletionsAdapter(model="stub", api_key="test", http_async_client=client, max_retries=0)
        with pytest.raises(Exception):
            await model.ainvoke("must not send", {"callbacks": [FailingCollector()]})
    assert sent == []
    assert model_request_recorder.get() is None
