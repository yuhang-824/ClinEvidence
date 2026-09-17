"""单 Agent 边界的负向回归。"""

from dataclasses import fields
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from server.routers.agent_router import AgentCreate, AgentUpdate
from yuxi.agents.buildin import agent_manager
from yuxi.agents.buildin.chatbot.context import ChatBotContext
from yuxi.repositories.agent_repository import user_can_access_agent, resolve_agent_is_subagent
from yuxi.services.agent_run_manifest_service import prepare_run_execution


def test_subagent_backend_and_configuration_are_absent():
    """真实发现和配置schema不可恢复退役后端。"""
    assert agent_manager.get_agent("SubAgentBackend") is None
    assert "subagents" not in {item.name for item in fields(ChatBotContext)}
    for payload in [{"backend_id": "SubAgentBackend"}, {"is_subagent": True}]:
        with pytest.raises(ValidationError):
            AgentCreate(name="test", **payload)
    with pytest.raises(ValidationError):
        AgentUpdate(is_subagent=True)


@pytest.mark.parametrize("backend_id,is_subagent", [("SubAgentBackend", False), ("ChatbotAgent", True)])
def test_retired_agent_is_denied_even_for_superadmin(backend_id, is_subagent):
    """历史标记与后端任一个为子Agent都不能继续执行。"""
    agent = SimpleNamespace(slug="old-worker", backend_id=backend_id, is_subagent=is_subagent)
    assert not user_can_access_agent(SimpleNamespace(role="superadmin"), agent)
    with pytest.raises(ValueError, match="单 Agent"):
        resolve_agent_is_subagent(backend_id, is_subagent)


@pytest.mark.asyncio
async def test_old_subagent_run_is_rejected_before_loading_context():
    """旧队列或替代调用路径也不能准备子运行上下文。"""
    with pytest.raises(ValueError, match="单 Agent"):
        await prepare_run_execution(
            run=SimpleNamespace(run_type="subagent"), user=None, db=None, workdir_binding=None, worker_id="test"
        )
