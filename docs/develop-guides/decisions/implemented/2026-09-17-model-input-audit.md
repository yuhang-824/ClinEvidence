# 模型请求输入审计

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/model_message_audit_service.py

## 问题

开发者在对话 Debug 中只能看到模型输出，无法核查本轮实际 system、历史消息、工具 schema 与请求参数。

## 决策

在 OpenAI 兼容模型异步 HTTP 发送前保存 JSON 请求体，复用现有 PostgreSQL Message 审计和 Run lease。LangChain 模型调用 ID 关联请求与输出；请求头、认证信息不进入记录。调试接口沿用超级管理员及所属会话授权，普通聊天历史不返回输入。新增输入页签，历史或未接入协议明确显示未采集。

目标覆盖当前 DeepSeek、MiniMax、LM Studio、vLLM 的聊天协议、多轮工具续答、失败和 SDK 重试。非目标为 embedding/rerank、同步脚本、Anthropic/Gemini 协议审计和历史输入重建。完整消息会增加本地数据库容量。

## 替代方案

仅保存配置提示词无法反映中间件及协议转换后的输入；仅用 Langfuse 依赖额外服务。另建日志表会重复现有 Run 审计归属，因此扩展既有记录。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 保存请求体与模型端接收一致 | schema 或工具历史丢失、跨调用串线 | models/chat.py 与审计 service | `pytest test/unit/agents/test_model_input_audit.py`；`CLINEVIDENCE_SCOPE_SMOKE=1 pytest test/e2e/test_clinevidence_single_agent.py` | 并发调用、失败请求、两轮工具续答、取消/关闭流、持久化失败阻止发送 | Passed：6 项协议测试和 1 项真实 worker E2E |
| 输入受 Run lease 与会话权限约束 | 输入跨用户泄漏或失效 worker 写入 | 审计 repository 与 conversation service | `pytest test/integration/services/test_agent_run_lease.py -k model_input_audit`；上述 E2E | 错误 owner、普通用户、不同会话 | Passed：2 项真实 PG；HTTP 验证 403/404 和 history 不含输入 |
| Debug 可读输入并明确历史缺失 | 误显示配置副本或旧记录崩溃 | MessageDebugPanel.vue | `pnpm --dir web test:unit`、`lint:check`、`build` 与浏览器 | 无输入的旧记录、长提示、多块 system | Passed：333 项 web unit；后续多块 system 的相关 36 项重跑通过；真实 LM Studio 新调用可见完整输入，复制有成功反馈 |

真实浏览器在桌面宽度与面板最大化下验证了 system、messages、12 个工具定义及参数，页面截图由浏览器验证输出。线上 DeepSeek / MiniMax 和 vLLM 服务未实际调用；使用同一 OpenAI 协议 transport 与 worker 合成端点验证。真实审批恢复的输入链路未单独 E2E 验证，恢复入口的 callback 装配经代码审查；取消和提前关闭经过真实 SDK 流的针对性测试。

`pytest test/unit -m "not slow" --show-capture=no` 最终结果为 2069 passed、53 skipped、7 subtests passed；跳过项不作为功能通过证据。`ruff check package server`、`python scripts/verify_engineering_contracts.py`、`python -m unittest scripts.test_verify_engineering_contracts`（62 项）和 `git diff --check` 通过。文档构建及相对链接核对通过。构建保留既有大 chunk 提示，测试环境 pytest 缓存目录不可写提示不影响断言结果。

## 后果

输入包含用户提交的内容，应与现有会话数据一同管理。保存完整请求增加空间；本次不截断 system 或工具 schema。HTTP 请求 hook 只读取 JSON 正文，不保存 URL 或 headers。未采集协议不得冒充完整记录。

SDK 内部 HTTP 客户端被用于挂载 hook，复用其连接池所有权；升级 OpenAI / LangChain 依赖时须运行协议测试。输入发送前先提交数据库，审计不可写会阻止请求发送。正文存入既有 Message metadata，无新增表或 schema 迁移；已有调试列表最多读取 500 条，长会话的完整输入会增加该接口负载。
