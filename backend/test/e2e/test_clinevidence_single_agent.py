"""本地确定性模型验证单 Agent 的真实 HTTP、worker 与持久结果。"""

import asyncio
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import delete, select
from yuxi.repositories.user_repository import UserRepository
from yuxi.repositories.agent_run_repository import AgentRunRepository
from yuxi.services.agent_run_service import enqueue_agent_run
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User, AgentRun, Message, Department, Conversation, SubagentThread
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("CLINEVIDENCE_SCOPE_SMOKE") != "1", reason="显式启用合成端到端测试"),
]
EXPECTED = "CLINEVIDENCE_SINGLE_AGENT_OK"


class ChatStub(BaseHTTPRequestHandler):
    """记录模型实际可见工具，并输出确定性 SSE 回答。"""

    seen_tools = []

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen_tools.append([item["function"]["name"] for item in payload.get("tools", [])])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for delta, finish in [({"role": "assistant", "content": EXPECTED}, None), ({}, "stop")]:
            chunk = {
                "id": "chatcmpl-local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "stub",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *_args):
        pass


async def test_single_agent_runtime_and_retired_api(tmp_path):
    """普通对话实际完成且模型无委派工具，退役后端及旧配置无法创建。"""
    pg_manager.initialize()
    uid = "pytest_single_" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    provider_id = "pytest-single-" + uuid.uuid4().hex[:8]
    thread_id = agent_slug = child_thread_id = None
    provider_created = False
    ChatStub.seen_tools = []
    server = ThreadingHTTPServer(("0.0.0.0", 0), ChatStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    async with pg_manager.get_async_session_context() as db:
        department = Department(name=uid)
        db.add(department)
        await db.flush()
        department_id = department.id
        user = User(
            uid=uid,
            username=uid,
            role="superadmin",
            department_id=department_id,
            password_hash=AuthUtils.hash_password(password),
        )
        db.add(user)
        await db.commit()
        user_id = user.id
    async with httpx.AsyncClient(base_url=os.getenv("TEST_BASE_URL", "http://localhost:5050"), timeout=60) as client:

        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            assert response.is_success, f"{method} {path}: {response.status_code} {response.text}"
            return response.json()

        try:
            token = await request("POST", "/api/auth/token", data={"username": uid, "password": password})
            client.headers["Authorization"] = "Bearer " + token["access_token"]
            backends = await request("GET", "/api/agent/backends")
            assert {item["backend_id"] for item in backends["backends"]} == {"ChatbotAgent"}
            assert (await client.get("/api/agent/backends/SubAgentBackend")).status_code == 404
            for invalid in [{"backend_id": "SubAgentBackend"}, {"is_subagent": True}]:
                assert (await client.post("/api/agent", json={"name": "retired", **invalid})).status_code == 422
            agents = await request("GET", "/api/agent?include_subagents=true")
            assert all(
                not item.get("is_subagent") and item["backend_id"] != "SubAgentBackend" for item in agents["agents"]
            )
            await request(
                "POST",
                "/api/system/model-providers",
                json={
                    "provider_id": provider_id,
                    "display_name": "Synthetic single Agent test",
                    "provider_type": "openai",
                    "base_url": f"http://api:{server.server_port}/v1",
                    "api_key": "local-test-only",
                    "capabilities": ["chat"],
                    "enabled_models": [{"id": "stub", "type": "chat"}],
                },
            )
            provider_created = True
            created = await request(
                "POST",
                "/api/agent",
                json={
                    "name": uid,
                    "slug": uid,
                    "config_json": {
                        "context": {
                            "model": provider_id + ":stub",
                            "system_prompt": "Return the test marker.",
                            "tools": [],
                            "skills": [],
                            "knowledges": [],
                            "mcps": [],
                            "subagents": ["general-purpose"],
                        }
                    },
                },
            )
            agent_slug = created["agent"]["slug"]
            assert "subagents" not in created["agent"]["config_json"].get("context", {})
            thread = await request("POST", "/api/chat/thread", json={"agent_id": agent_slug, "title": uid})
            thread_id = thread.get("thread_id") or thread["id"]
            submitted = await request(
                "POST", "/api/agent/runs", json={"agent_slug": agent_slug, "thread_id": thread_id, "query": EXPECTED}
            )
            run_id = submitted["run_id"]
            for _ in range(120):
                run = (await request("GET", f"/api/agent/runs/{run_id}"))["run"]
                if run["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                    break
                await asyncio.sleep(1)
            assert run["status"] == "completed", run
            result = await request("GET", f"/api/agent/runs/{run_id}/result")
            assert result["output"] == EXPECTED
            assert ChatStub.seen_tools
            assert all(
                not ({"task", "subagent_start", "subagent_status", "subagent_await", "subagent_cancel"} & set(names))
                for names in ChatStub.seen_tools
            )
            async with pg_manager.get_async_session_context() as db:
                saved = (await db.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
                output = (await db.execute(select(Message).where(Message.id == saved.output_message_id))).scalar_one()
                assert saved.status == "completed" and saved.run_type == "chat"
                assert output.run_id == run_id and output.content == EXPECTED
                children = (
                    (await db.execute(select(AgentRun).where(AgentRun.created_by_run_id == run_id))).scalars().all()
                )
                assert children == []

                # 模拟升级前遗留的pending子运行；正常服务已无法创建该类型。
                legacy_id = str(uuid.uuid4())
                legacy_request = str(uuid.uuid4())
                parent = (
                    await db.execute(select(Conversation).where(Conversation.id == saved.conversation_id))
                ).scalar_one()
                child_thread_id = str(uuid.uuid4())
                child = Conversation(
                    thread_id=child_thread_id,
                    uid=uid,
                    agent_id=agent_slug,
                    project_id=parent.project_id,
                    title="Synthetic legacy child",
                )
                db.add(child)
                await db.flush()
                relation = SubagentThread(
                    uid=uid,
                    parent_conversation_id=parent.id,
                    child_conversation_id=child.id,
                    child_thread_id=child_thread_id,
                    subagent_slug=agent_slug,
                    created_by_run_id=run_id,
                )
                db.add(relation)
                await db.flush()
                legacy_input = Message(
                    conversation_id=child.id,
                    role="user",
                    content="Synthetic legacy",
                    message_type="text",
                    request_id=legacy_request,
                    delivery_status="complete",
                    extra_metadata={},
                )
                db.add(legacy_input)
                await db.flush()
                await AgentRunRepository(db).create_run(
                    run_id=legacy_id,
                    conversation_thread_id=child_thread_id,
                    agent_slug=agent_slug,
                    uid=uid,
                    request_id=legacy_request,
                    conversation_id=child.id,
                    subagent_thread_relation_id=relation.id,
                    run_type="subagent",
                    created_by_run_id=run_id,
                    input_message_id=legacy_input.id,
                    input_payload={"runtime": {"tool_call_id": "legacy-call", "subagent_name": "Retired"}},
                )
                legacy_input.run_id = legacy_id
                await db.commit()
            await enqueue_agent_run(legacy_id)
            for _ in range(60):
                legacy = (await request("GET", f"/api/agent/runs/{legacy_id}"))["run"]
                if legacy["status"] == "failed":
                    break
                await asyncio.sleep(1)
            async with pg_manager.get_async_session_context() as db:
                legacy = (await db.execute(select(AgentRun).where(AgentRun.id == legacy_id))).scalar_one()
                assert legacy.status == "failed" and legacy.error_type == "invalid_run_type"
                assert legacy.worker_id is None and legacy.lease_expires_at is None
            child_state = await request("GET", f"/api/chat/thread/{child_thread_id}/state")
            assert child_state["subagent_run"]["status"] == "failed"
            historical = await request("GET", f"/api/chat/thread/{thread_id}/history")
            assert any(item.get("content") == EXPECTED for item in historical["history"])
            assert (await request("GET", f"/api/agent/runs/{run_id}/result"))["output"] == EXPECTED
        finally:
            try:
                if child_thread_id:
                    await request("DELETE", f"/api/chat/thread/{child_thread_id}")
                if thread_id:
                    await request("DELETE", f"/api/chat/thread/{thread_id}")
                if agent_slug:
                    await request("DELETE", f"/api/agent/{agent_slug}")
                if provider_created:
                    await request("DELETE", f"/api/system/model-providers/{provider_id}")
            finally:
                async with pg_manager.get_async_session_context() as db:
                    attached = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
                    await UserRepository(db).delete_for_admin(attached)
                    attached.department_id = None
                    await db.flush()
                    await db.execute(delete(Department).where(Department.id == department_id))
                    await db.commit()
                async with pg_manager.get_async_session_context() as db:
                    saved = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
                    assert saved.is_deleted == 1 and saved.password_hash == "DELETED"
                server.shutdown()
                server.server_close()
                await pg_manager.close()
