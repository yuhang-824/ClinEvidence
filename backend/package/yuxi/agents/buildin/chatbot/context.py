from dataclasses import dataclass, field

from yuxi.agents.context import BaseContext


@dataclass(kw_only=True)
class ChatBotContext(BaseContext):
    """单 Agent 的对话运行上下文。"""

    requires_patient: bool = field(
        default=False,
        metadata={
            "name": "诊疗模式(绑定患者)",
            "description": "开启后,该智能体的会话必须绑定一名患者且创建后不可更换,并启用患者病历检索工具。",
            "type": "bool",
        },
    )
