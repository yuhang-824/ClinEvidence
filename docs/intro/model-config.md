# 配置模型

ClinEvidence 在“智能体 → 模型供应商”中统一管理聊天、嵌入和重排模型。只有管理员可以新增或修改供应商；普通用户可以在有权限的地方选择已经启用的模型。

## 配置顺序

1. 打开“智能体 → 模型供应商”。
2. 添加模型服务，或打开一个内置服务。
3. 填写 API 地址和凭证，选择供应商能力。
4. 在供应商的“模型配置”中获取服务模型，或手动添加模型。
5. 对模型执行连接测试，再把它选为智能体或知识库使用的模型。

供应商停用后，其模型不会进入运行时模型缓存。Web 管理页面会在系统默认模型仍引用某个供应商或模型时阻止删除或停用，先切换默认模型再修改；直接调用管理 API 时也应先检查并替换默认引用，不能依赖页面保护。

## 凭证怎么保存

供应商支持两种凭证来源：

| 来源 | 配置位置 | 适用场景 |
| --- | --- | --- |
| 环境变量 | 供应商的“API Key Env”填写变量名，密钥放在 API/worker 环境中 | 生产环境，推荐 |
| 直接填写 | 供应商的“API Key”字段 | 本地测试或明确接受数据库保存凭证的环境 |

使用环境变量时，字段里填写变量名，例如 `SILICONFLOW_API_KEY`，不要把密钥本身写进文档。修改容器环境变量后需要重新创建读取它的 API 和 worker；供应商页面的保存不会替你更新容器：

```bash
docker compose up -d --force-recreate api worker
```

## 模型服务范围

ClinEvidence 的线上聊天入口为 DeepSeek 与 MiniMax 国内。LM Studio 和 vLLM 连接本地已启动的推理服务。SiliconFlow（国内及国际）、DashScope 和 OpenRouter 保留各自原有的线上 Embedding / 重排能力；所有供应商已有的密钥、向量模型和知识库引用保持不变。管理员可添加额外模型服务，用于独立部署的向量或重排端点。

聊天模型没有默认凭据或自动可用状态。先配置供应商、启用模型并测试连接，再到“设置 → 基本设置”选择默认对话与快速响应模型。旧线上聊天供应商停用后，其显式 Agent 模型引用需要重新选择；系统不会替用户切换至另一家云服务。

### LM Studio

在 LM Studio 中加载模型并启动 Local Server。Base URL 通常为 `http://localhost:1234/v1`。在 ClinEvidence 的 LM Studio 卡片保存地址并启用，再进入“管理模型”获取服务模型。通用模型列表未标明用途时，请核对模型类型，向量模型可手动添加，填写实际维度后执行连接测试。

LM Studio 未开启鉴权时 API Key 可留空；开启鉴权时填写服务生成的 Token。连接的是本地服务，ClinEvidence 不负责下载或加载权重。聊天和向量协议见 [LM Studio 官方文档](https://lmstudio.ai/docs/developer/openai-compat)。

### vLLM

先在推理服务器启动 vLLM 并加载所需模型，然后在 vLLM 卡片填写后端可访问的 OpenAI 兼容 Base URL，例如 `http://inference-host:8000/v1`。模型 ID 使用服务端实际暴露的名称；聊天、向量和重排模型通常需要各自的服务实例。工具调用还取决于所选模型、聊天模板和服务端工具解析配置。

本地预设的向量地址留空时使用 Base URL 加 `/embeddings`，vLLM 重排地址留空时使用 Base URL 加 `/rerank`。独立部署时填写完整地址，或在单个模型中指定独立的完整请求 URL。不要为未部署的能力填写一个虚构模型。接口协议见 [vLLM 官方文档](https://docs.vllm.ai/en/v0.8.3/serving/openai_compatible_server.html)。

### 容器到本地服务的地址

API 和 worker 必须都能访问模型服务。Docker 部署会将 `http://localhost` / `http://127.0.0.1` 转换到 Docker 宿主机。当 Docker 运行在 WSL、模型运行在 Windows 时，填写可从 WSL 访问的 Windows 主机 IP；Windows 模型服务需监听可访问的网卡。WSL 地址变化后需要更新配置。模型发现与实际调用使用相同地址转换。

### 线上向量与重排

线上 Embedding / 重排和本地配置可以同时存在，通过基本设置和知识库配置选择。更换已有知识库的 Embedding 模型时，创建使用新模型的知识库并重新导入文档，不复用旧向量索引；仅维度相同也不代表模型兼容。重排模型不拥有持久向量，可以独立配置。

MiniMax 国内使用 [OpenAI 兼容接口](https://platform.minimax.cn/docs/api-reference/text-openai-api)，在管理模型中手动填写账户支持的 ID，例如 `MiniMax-M2.5`。DeepSeek 可以通过服务模型列表发现可用模型。两者均需要对应平台凭据，其他供应商密钥不能通用。

## 添加和启用模型

### 从远程列表添加

打开供应商的模型配置，点击“获取服务模型”，从返回列表中选择模型。远程列表只用于发现候选项，不会自动启用模型；确认添加后，模型才会进入运行时。

### 手动添加

点击“手动添加”，填写模型 ID 和类型：

- `chat`：聊天和智能体运行。
- `embedding`：知识库向量化，需要填写供应商规格中的向量维度。
- `rerank`：对检索候选结果重排。

知识库创建后，嵌入模型和向量维度属于索引的一部分。更换嵌入模型或维度后，需要按知识库流程重建索引，不能把不同向量空间的结果混在一起。

### 模型标识

运行时使用 `provider_id:model_id`，只按第一个冒号分隔供应商和模型 ID。模型 ID 可以包含斜杠，例如：

```text
siliconflow-cn:Pro/BAAI/bge-m3
```

页面中的模型选择器会显示供应商名称和模型名称；在 API、日志或配置快照中排查问题时，使用完整 spec。

## 配置聊天模型的请求参数

OpenAI Completions API 兼容供应商的 `chat` 模型可以配置“模型请求参数 JSON”。Yuxi 会把它作为 OpenAI SDK 的 `extra_body` 合并到请求体顶层，用于支持不同供应商的思考或推理参数。

当前允许的顶层字段是：

| 字段 | 常见用途 |
| --- | --- |
| `enable_thinking` | 开关式思考配置 |
| `thinking_budget` | 思考 Token 预算 |
| `thinking` | 供应商的思考配置对象 |
| `reasoning` | 推理配置对象 |
| `reasoning_effort` | OpenAI 风格的推理强度 |

示例：

```json
{
  "enable_thinking": true,
  "thinking_budget": 1024
}
```

白名单只限制顶层字段，字段内部结构和可用取值由供应商校验。参数是否生效取决于模型和供应商接口，Yuxi 不会把不支持的字段转换成另一种格式。Anthropic、Gemini 等非 OpenAI 兼容供应商不能使用这组 `extra_body` 覆盖。

## 移除旧模型配置

在供应商的已启用模型列表中移除模型。Web 页面不会让当前默认模型直接移除，先在系统配置中换用其他模型再操作；直接调用管理 API 时需要自行保证默认引用仍然有效。知识库的嵌入模型变更后，按知识库页面重新建立索引。

旧版 `provider/model`、旧知识库 JSON 模型字段以及配置文件中的 `model_names`、`embed_model_names`、`reranker_names` 不属于当前运行时来源。升级后如果历史 Agent 或知识库仍保存旧格式，请在界面重新选择模型并保存；知识库嵌入模型变更后还要重建索引。

## Ollama

当前版本没有 Ollama provider 类型，也没有 Ollama embedding 运行时适配。已有 Ollama embedding 知识库需要选择新的嵌入模型并重建索引。

## 排查模型不可用

按下面顺序检查：

1. 供应商是否启用，模型是否位于“已启用模型”列表。
2. API 地址是否能从 API/worker 容器访问。
3. API Key Env 对应的变量是否存在，或直接凭证是否正确。
4. 模型类型、嵌入维度和供应商能力是否匹配。
5. 在供应商详情中执行连接测试，并查看 API/worker 日志中的错误。

模型连接测试能证明该次请求得到响应，不能证明所有模型参数、工具调用或知识库索引都适配；这些行为仍需在实际 Agent 或知识库链路中验证。
