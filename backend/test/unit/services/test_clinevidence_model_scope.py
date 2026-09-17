"""模型供应商收敛与本地接入的负向回归。"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from yuxi.models.providers.builtin import BUILTIN_PROVIDERS, RETIRED_PROVIDER_IDS, RETRIEVAL_ONLY_PROVIDER_IDS
from yuxi.models.providers.cache import ModelCache, ModelInfo
from yuxi.storage.postgres.models_business import ModelProvider
from yuxi.models.providers.service import _normalize_payload, check_credential_status, resolve_api_key


def test_builtin_scope_and_disabled_unconfigured_models():
    """只提供两个线上与两个本地模板，不虚构已加载模型。"""
    assert {p["provider_id"] for p in BUILTIN_PROVIDERS if p["provider_id"] not in RETRIEVAL_ONLY_PROVIDER_IDS} == {
        "deepseek",
        "minimax-cn",
        "lmstudio",
        "vllm",
    }
    assert all(
        "chat" not in p["capabilities"] for p in BUILTIN_PROVIDERS if p["provider_id"] in RETRIEVAL_ONLY_PROVIDER_IDS
    )


@pytest.mark.parametrize("provider_id", sorted(RETIRED_PROVIDER_IDS))
def test_retired_provider_cannot_be_recreated(provider_id):
    """改变旧供应商 URL 也不能重新启用其退役标识。"""
    with pytest.raises(ValueError, match="已移除"):
        _normalize_payload({"provider_id": provider_id, "display_name": "retired", "base_url": "http://localhost"})


@pytest.mark.parametrize("provider_id", ["lmstudio", "vllm"])
def test_local_credentials_and_endpoint_derivation(provider_id):
    """本地无鉴权可连接，显式密钥与独立向量端点优先。"""
    provider = SimpleNamespace(
        provider_id=provider_id,
        is_enabled=True,
        api_key=None,
        api_key_env=None,
        base_url="http://localhost:8123/v1/",
        embedding_base_url=None,
        rerank_base_url=None,
    )
    assert resolve_api_key(provider) == "local-no-auth"
    assert check_credential_status(provider) == "ok"
    assert ModelCache._get_base_url_for_type(provider, "embedding") == "http://localhost:8123/v1/embeddings"
    assert ModelCache._get_base_url_for_type(provider, "rerank") == "http://localhost:8123/v1/rerank"
    provider.api_key = "test-token"
    assert resolve_api_key(provider) == "test-token"
    provider.embedding_base_url = "http://embedding:8001/v1/embeddings"
    assert ModelCache._get_base_url_for_type(provider, "embedding") == provider.embedding_base_url


def test_old_redis_and_enabled_database_rows_cannot_restore_retired_models(monkeypatch):
    """旧缓存与仍被标记启用的旧行都不能让模型重新可调用。"""
    from yuxi.models.providers import cache as module

    models = [
        ModelInfo(
            provider_id=p,
            model_id=t,
            model_type=t,
            display_name=t,
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider_type="openai",
        )
        for p, t in [
            ("siliconflow-cn", "chat"),
            ("siliconflow", "embedding"),
            ("openai", "chat"),
            ("openai", "embedding"),
        ]
    ]

    @contextmanager
    def redis():
        yield SimpleNamespace(get=lambda _key: json.dumps({m.spec: m.to_dict() for m in models}))

    monkeypatch.setattr(module, "sync_redis_client", redis)
    cache = ModelCache()
    assert {m.spec for m in cache.get_all_specs()} == {"siliconflow:embedding", "openai:embedding"}
    written = {}
    monkeypatch.setattr(cache, "_save_cache", written.update)
    cache.rebuild(
        [
            ModelProvider(
                provider_id="openai",
                is_enabled=True,
                base_url="https://example.invalid/v1",
                provider_type="openai",
                api_key="test-key",
                enabled_models=[{"id": "chat", "type": "chat"}, {"id": "embedding", "type": "embedding"}],
            )
        ]
    )
    assert set(written) == {"openai:embedding"}


@pytest.mark.parametrize("provider_id", ["siliconflow-cn", "alibaba-cn"])
def test_retrieval_provider_rejects_chat_but_keeps_vectors(provider_id):
    """线上向量通道保留，但不能借该供应商重新启用聊天。"""
    data = {
        "provider_id": provider_id,
        "display_name": "Retrieval",
        "base_url": "https://example.invalid/v1",
        "capabilities": ["embedding", "rerank"],
        "enabled_models": [{"id": "embed", "type": "embedding"}],
    }
    assert _normalize_payload(data)["enabled_models"][0]["type"] == "embedding"
    data["capabilities"].append("chat")
    data["enabled_models"].append({"id": "chat", "type": "chat"})
    with pytest.raises(ValueError, match="仅保留"):
        _normalize_payload(data)


@pytest.mark.parametrize("provider_id", ["lmstudio", "vllm"])
def test_local_explicit_missing_token_does_not_fall_back_to_no_auth(provider_id, monkeypatch):
    """显式选择环境变量鉴权后，缺少凭据必须可见。"""
    monkeypatch.delenv("MISSING_LOCAL_MODEL_TOKEN", raising=False)
    provider = SimpleNamespace(
        provider_id=provider_id, is_enabled=True, api_key=None, api_key_env="MISSING_LOCAL_MODEL_TOKEN"
    )
    assert resolve_api_key(provider) is None
    assert check_credential_status(provider) == "warning"
