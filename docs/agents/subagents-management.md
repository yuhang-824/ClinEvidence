# 单 Agent 执行范围

ClinEvidence 的诊疗与质控由单 Agent 调用知识库及工具完成。子 Agent 后端、委派工具、默认子 Agent 和配置入口已移除；旧子 Agent 记录不能创建新的运行。

历史子线程、工具结果和运行状态保留只读展示与恢复收尾，不代表仍提供委派功能。多个独立的提示词配置可分别使用，相互之间不能委派。配置方式见[配置智能体](./agents-config.md)，执行约束见[Agent 运行机制](../mechanisms/agent-runtime.md)。
