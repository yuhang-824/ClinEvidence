"""真实 PostgreSQL 升级与本地 HTTP 模型协议验证，不请求云端模型。"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from yuxi.config.options import ensure_options_in_db, system_options
from yuxi.models.chat import load_chat_model
from yuxi.models.embed import select_embedding_model
from yuxi.models.providers.cache import ModelCache, model_cache
from yuxi.models.providers.service import (
    ensure_builtin_model_providers_in_db,
    fetch_remote_models,
    get_all_model_providers,
)
from yuxi.models.rerank import get_reranker
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import ConfigOption, ModelProvider

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("CLINEVIDENCE_SCOPE_SMOKE") != "1", reason="显式启用真实数据库验证"),
]


async def test_upgrade_disables_old_providers_and_clears_only_retired_defaults():
    """事务内模拟旧配置，幂等同步后回读真实行，退出回滚。"""
    pg_manager.initialize()
    try:
        async with pg_manager.get_async_session_context() as db:
            try:
                old = (
                    await db.execute(select(ModelProvider).where(ModelProvider.provider_id == "fluxionai"))
                ).scalar_one_or_none()
                if old is None:
                    old = ModelProvider(
                        provider_id="fluxionai", display_name="retired", base_url="https://example.invalid"
                    )
                    db.add(old)
                old.is_enabled = True
                retrieval = (
                    await db.execute(select(ModelProvider).where(ModelProvider.provider_id == "openai"))
                ).scalar_one_or_none()
                if retrieval is None:
                    retrieval = ModelProvider(
                        provider_id="openai", display_name="Old retrieval", base_url="https://example.invalid/v1"
                    )
                    db.add(retrieval)
                retrieval.is_enabled = True
                retrieval.api_key = "synthetic-key"
                retrieval.capabilities = ["chat", "embedding", "rerank"]
                vectors = [
                    {
                        "id": "e",
                        "type": "embedding",
                        "dimension": 8,
                        "base_url_override": "http://embedding/v1/embeddings",
                    },
                    {"id": "r", "type": "rerank"},
                ]
                retrieval.enabled_models = [{"id": "c", "type": "chat"}, *vectors]
                await ensure_options_in_db(db)
                options = (
                    await db.execute(select(ConfigOption).where(ConfigOption.key == system_options.key))
                ).scalar_one()
                options.value = {
                    "default_model": "siliconflow-cn:old",
                    "embed_model": "siliconflow-cn:embed",
                    "fast_model": "lmstudio:keep",
                    "reranker": "vllm:keep",
                }
                await db.flush()
                for _ in range(2):
                    await ensure_builtin_model_providers_in_db(db)
                    await ensure_options_in_db(db)
                await db.refresh(old)
                await db.refresh(options)
                assert old.is_enabled is False
                await db.refresh(retrieval)
                assert retrieval.is_enabled is True
                assert retrieval.enabled_models == vectors
                assert retrieval.api_key == "synthetic-key"
                assert set(retrieval.capabilities) == {"embedding", "rerank"}
                assert options.value == {
                    "default_model": "",
                    "embed_model": "siliconflow-cn:embed",
                    "fast_model": "lmstudio:keep",
                    "reranker": "vllm:keep",
                }
                providers = {p.provider_id: p for p in await get_all_model_providers(db)}
                assert "fluxionai" not in providers
                assert "openai" in providers
                assert {"deepseek", "minimax-cn", "lmstudio", "vllm"} <= providers.keys()
            finally:
                await db.rollback()
    finally:
        await pg_manager.close()


@pytest.mark.parametrize("provider_id", ["lmstudio", "vllm"])
async def test_local_model_discovery_chat_embedding_and_rerank(provider_id, monkeypatch):
    """SDK 经真实 HTTP 连接本地服务；回读实际请求和协议结果。"""
    requests = []

    class Server(BaseHTTPRequestHandler):
        """确定性协议替身，不宣称验证真实权重推理质量。"""

        def respond(self, body):
            """返回 JSON 协议响应。"""
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            """返回当前模型列表。"""
            requests.append((self.path, None))
            self.respond({"data": [{"id": "local-chat", "object": "model"}]})

        def do_POST(self):
            """处理聊天、向量和重排协议。"""
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, payload))
            if self.path == "/v1/chat/completions":
                self.respond(
                    {
                        "id": "test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "local-chat",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "local OK"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
            elif self.path == "/v1/embeddings":
                self.respond({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})
            elif self.path == "/v1/rerank":
                self.respond({"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]})
            else:
                self.send_error(404)

        def log_message(self, *_args):
            """测试不输出请求日志。"""

    # 测试服务与 SDK 同进程宿主；另有 unit 验证容器地址转换。
    monkeypatch.setenv("RUNNING_IN_DOCKER", "false")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = ModelProvider(
        provider_id=provider_id,
        display_name="Local",
        is_enabled=True,
        provider_type="openai",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        models_endpoint="/models",
        capabilities=["chat", "embedding", "rerank"],
        enabled_models=[
            {"id": "local-chat", "type": "chat"},
            {"id": "embed", "type": "embedding", "dimension": 3},
            {"id": "rank", "type": "rerank"},
        ],
    )
    cache = ModelCache()
    saved = {}
    monkeypatch.setattr(cache, "_save_cache", saved.update)
    cache.rebuild([provider])
    monkeypatch.setattr(model_cache, "get_model_info", saved.get)
    try:
        assert (await fetch_remote_models(provider))[0]["id"] == "local-chat"
        assert (await load_chat_model(f"{provider_id}:local-chat").ainvoke("Test")).text == "local OK"
        assert await select_embedding_model(f"{provider_id}:embed").aencode(["Test"]) == [[1.0, 0.0, 0.0]]
        if provider_id == "vllm":
            ranker = get_reranker("vllm:rank")
            try:
                assert await ranker.acompute_score(["query", ["a", "b"]], normalize=False) == [0.1, 0.9]
            finally:
                await ranker.aclose()
        assert [path for path, _ in requests] == ["/v1/models", "/v1/chat/completions", "/v1/embeddings"] + (
            ["/v1/rerank"] if provider_id == "vllm" else []
        )
    finally:
        server.shutdown()
        server.server_close()
