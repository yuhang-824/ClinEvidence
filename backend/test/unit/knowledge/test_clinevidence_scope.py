from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from server.routers import router
from yuxi.agents.skills.buildin import BUILTIN_SKILLS
from yuxi.agents.skills.service import user_can_access_skill
from yuxi.agents.toolkits.registry import _all_tool_instances
from yuxi.knowledge.base import KBNotFoundError
from yuxi.knowledge.runtime import KnowledgeBaseFactory
from yuxi.repositories.agent_repository import AgentRepository, user_can_access_agent
from yuxi.services.task_registry import _TASK_DEFINITIONS


def test_local_pdf_capabilities_exclude_retired_features(tmp_path):
    """真实装配保留文档与评测，退役能力无法重新发现或创建。"""
    app = FastAPI()
    app.include_router(router)
    paths = set(app.openapi()['paths'])
    assert any("/knowledge/" in path for path in paths)
    assert any("/evaluation/" in path for path in paths)
    assert not any("/graph/" in path or "/graph-build/" in path for path in paths)
    assert set(KnowledgeBaseFactory.get_available_types()) == {"milvus"}
    for kind in ("dify", "notion"):
        with pytest.raises(KBNotFoundError):
            KnowledgeBaseFactory.create(kind, str(tmp_path))
    assert "knowledge_graph_index" not in _TASK_DEFINITIONS
    assert {"knowledge_ingest", "knowledge_parse", "knowledge_index"} <= _TASK_DEFINITIONS.keys()
    tools = {tool.name for tool in _all_tool_instances}
    assert "web_search" not in tools
    assert "tavily_search" not in tools
    assert "/knowledge/files/fetch-url" not in paths
    skills = {spec.slug for spec in BUILTIN_SKILLS}
    assert "knowledge-base" in skills
    assert not {"deep-research", "mysql-reporter"} & skills


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["web-search", "deep-research", "research-explorer", "fact-verifier"])
async def test_retired_agent_cannot_be_loaded_or_authorized(slug):
    """即使遗留记录全局共享，管理员也不能继续执行退役预设。"""
    assert await AgentRepository(None).get_by_slug(slug) is None
    assert not user_can_access_agent(SimpleNamespace(role="superadmin"), SimpleNamespace(slug=slug))


@pytest.mark.parametrize("slug", ["deep-research", "mysql-reporter"])
def test_retired_builtin_skill_is_not_authorized(slug):
    """旧内置技能即使保存为启用也不能进入模型执行上下文。"""
    skill = SimpleNamespace(slug=slug, source_type="builtin", enabled=True)
    assert not user_can_access_skill(SimpleNamespace(role="superadmin"), skill)


@pytest.mark.asyncio
async def test_new_agent_slug_avoids_retired_names():
    """新建 Agent 不得生成已经禁止读取的旧预设标识。"""
    repo = AgentRepository(None)
    repo._slug_exists = AsyncMock(return_value=False)
    assert await repo._unique_slug("deep-research", "demo") == "deep-research-2"
