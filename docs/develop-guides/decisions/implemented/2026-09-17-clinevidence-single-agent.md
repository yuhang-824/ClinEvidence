# ClinEvidence 单 Agent 执行范围

状态：implemented
类型：simplification
Owner：backend/package/yuxi/agents/buildin/chatbot/graph.py

## 问题

本记录面向 ClinEvidence 开发者。诊疗与质控由单 Agent 调用知识库及工具完成，通用子 Agent 委派增加运行与配置复杂度。本决定承接[分批精简](../proposed/2026-09-17-clinevidence-scope-reduction.md)的 Agent 部分，MCP、调度和沙盒的后续取舍不在本批范围。

## 决策

唯一内置执行后端为 ChatbotAgent。图装配不包含委派中间件，移除 SubAgentBackend、子运行创建服务、默认子 Agent 安装和子运行状态 reducer。模型不再获得 task、subagent_start、subagent_status、subagent_await 或 subagent_cancel 工具。

AgentRepository 对旧子标记和旧后端同时拒绝访问，新建与更新 API 不接受子 Agent 标记。旧 subagents 配置通过声明字段过滤，不进入执行上下文。worker 与运行准备服务只接受 chat/resume，旧 pending 子运行显式失败。运行记录与父子关系的数据库结构保留，历史只读投影、取消及终态清理继续拥有其原有数据语义。

前端移除管理分组、配置选项及输入框子 Agent 提及。不同的提示词配置可以独立使用，相互之间不可委派。旧子线程、工具结果展示保留为历史阅读入口。deepagents 的文件系统、摘要及消息修复仍有真实消费者，因此保留依赖。

## 替代方案

只隐藏 UI 无法阻止模型委派。删除历史表会扩大到破坏性迁移。强制数据库只保留一个 Agent 会删除已有独立提示词配置，并非移除多 Agent 调用的必要条件。采用删除执行与配置能力、保留历史记录的方案。

## 后果

单 Agent 对话继续使用 PostgreSQL checkpoint、普通请求 FIFO、worker lease、取消、工具审批和原文引用。诊疗或质控可由提示词与工具编排区分；单 Agent 架构不等于医疗效果已验收。旧图谱与子 Agent 决策的当前边界以本记录和架构说明为准。

## 验证

旧能力不存在：单元测试覆盖真实后端发现、Context 字段、创建/更新 schema、管理员对旧记录的拒绝和旧 Run 准备失败。

`CLINEVIDENCE_SCOPE_SMOKE=1 uv run --no-sync pytest test/e2e/test_clinevidence_single_agent.py -q --show-capture=no` 通过。真实 HTTP、worker 与 PostgreSQL 完成普通请求，读取对应 Run 的 output_message_id 与最终 Message；本地确定性模型实际收到的工具不含委派且未创建子 Run。测试模拟具有父子关系的旧 pending 子 Run，投递真实队列后回读 failed / invalid_run_type、空 worker owner 和 lease，并确认历史子状态和正常回答仍可读取。合成账号经 attached repository 软删除后回读验证，测试供应商、Agent 和线程清理。测试不调用外部模型，不评估医学语义质量。

Runtime System Tests workflow 显式开启并执行该端到端测试。全量后端非 slow unit 2010 passed、53 skipped；前端 332 项 unit、lint 和 build 通过；CLI Agent 命令 16 项通过。工程契约、62 项契约测试及文档构建通过。实际浏览器验证管理页面无子 Agent 分组，新建后端仅有智能助手，CLI 详情无 Subagents 行。远端 CI 已接线，本地交付不宣称远端 CI 已运行。

重新引入条件：有明确需求证明单 Agent 与现有工具无法完成目标，通过独立提案重新定义执行、权限、恢复及数据边界。
