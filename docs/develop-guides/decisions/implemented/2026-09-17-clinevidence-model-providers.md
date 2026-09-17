# ClinEvidence 模型供应商范围

状态：implemented
类型：simplification
Owner：backend/package/yuxi/models/providers/service.py

## 问题

开发者需要 DeepSeek、MiniMax 国内两个线上调试入口，以及 LM Studio、vLLM 本地服务接入。大量其他线上预设与 SiliconFlow 默认引用不符合院内使用方向。此工作负责连接已启动的推理服务，不负责下载权重、安装 GPU 驱动或评估模型医疗质量。

## 决策

聊天预设只保留 deepseek、minimax-cn、lmstudio、vllm；siliconflow-cn、alibaba-cn、siliconflow、openrouter 保留原有线上 Embedding / 重排模板。所有旧供应商中已有向量配置和凭据保留，仅剔除聊天能力和聊天模型；没有向量能力的旧供应商停用并隐藏。旧标识只能配置向量与重排，运行时拒绝其聊天模型。管理员自定义 OpenAI 兼容服务继续用于多个本地端点，不把该配置边界当成网络出口防火墙。

只清空退役聊天供应商的系统对话与快速模型引用，保留 Embedding / 重排的默认值、密钥及已有模型配置。Agent 与知识库已有显式模型引用不自动替换，避免错误复用向量索引；调用时报告模型不可用。新聊天预设默认停用；SiliconFlow 向量模板延续已有启用默认值，模型需按服务实际返回或手动 ID 配置。本地无鉴权服务无需真实 API Key；开启鉴权时使用填写的凭据。LM Studio 支持聊天和向量，vLLM 另支持重排，地址可以分别配置。

## 替代方案

仅删模板会留下数据库记录；只隐藏页面仍允许旧模型调用。直接改用 DeepSeek 默认模型会在缺少凭据时制造可用假象。采用显式待配置和持久化停用，用户需重新选择默认模型；旧知识库更换向量模型必须重新建库和索引。

## 后果

其他线上聊天供应商的历史凭据不销毁，已有向量服务继续可管理和调用。系统对话与快速模型的退役引用清空，管理员需明确选择可用聊天模型。Embedding / 重排默认值保持，旧知识库不会自动切换向量模型。更换向量模型必须创建新知识库并重新索引。

此范围是产品预设与旧能力收敛，不是网络出口防火墙；管理员自定义服务仍可配置兼容地址。未下载权重或安装 vLLM，不以连接测试证明医疗质量。

## 验证

旧能力不存在：unit 验证退役聊天配置被拒绝；完整合法 Redis 混合缓存和数据库行同时证明旧聊天过滤、旧向量保留。独立 Reviewer 的 mutation 删除缓存过滤后，该负向测试失败。真实 PostgreSQL 测试模拟旧供应商混合模型，幂等同步后回读向量配置、独立端点、维度、凭据及启用状态保留，聊天默认清空而向量默认保留；测试事务回滚。

`CLINEVIDENCE_SCOPE_SMOKE=1 uv run --no-sync pytest test/integration/services/test_clinevidence_model_providers.py test/e2e/test_clinevidence_single_agent.py -q --show-capture=no`：4 passed。覆盖真实本地 HTTP 的 LM Studio / vLLM 兼容聊天、向量、重排协议，真实管理 API 拒绝向量供应商重新配置聊天，并经 worker / PostgreSQL 回读单 Agent 最终回答。协议替身不证明真实 vLLM 权重推理。

`uv run --no-sync pytest test/unit -m "not slow" -q --show-capture=no`：2036 passed、53 skipped。最后补充的 DeepSeek / LM Studio / vLLM 推理与工具续答测试，以及 router / scope 回归合计 96 passed。后端 Ruff、前端 332 项 unit、lint、build、工程契约、62 项契约测试、文档构建通过。Runtime System Tests workflow 执行新增集成测试，本地不宣称远端 CI 已运行。

真实 LM Studio 服务经 API 容器调用，聊天返回 OK，Embedding 返回 768 维；已有加载模型被登记为本地可选项，不替换线上向量默认。浏览器验证对话/本地与向量/重排分组、启用卡片地址摘要和本地模型类型/维度。DeepSeek、MiniMax 未配置真实凭据，未调用云端；未发现可用的真实 vLLM 服务，保留待配置状态。

重新引入条件：新的明确需求与独立提案证明需要额外线上聊天预设，再修改注册、迁移、运行时过滤及回归证据。
