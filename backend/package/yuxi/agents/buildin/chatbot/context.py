from dataclasses import dataclass

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    """单 Agent 的对话运行上下文。"""
