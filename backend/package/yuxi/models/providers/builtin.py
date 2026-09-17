"""ClinEvidence 的线上调试与本地推理服务模板。"""

from typing import Any

# 保留退役标识用于已有部署的幂等停用；不再提供其配置模板。
RETIRED_PROVIDER_IDS = frozenset(
    [
        "fluxionai",
        "openai",
        "alibaba",
        "alibaba-coding-plan-cn",
        "alibaba-coding-plan",
        "zhipuai",
        "zhipuai-coding-plan",
        "zai",
        "zai-coding-plan",
        "xiaomi-token-plan-cn",
        "xiaomi",
        "kimi-for-coding",
        "moonshotai-cn",
        "moonshotai",
        "minimax",
        "modelscope",
        "opencode",
        "opencode-go",
    ]
)
RETRIEVAL_ONLY_PROVIDER_IDS = {"siliconflow-cn", "alibaba-cn", "siliconflow", "openrouter"}
RETIRED_CHAT_PROVIDER_IDS = RETIRED_PROVIDER_IDS | RETRIEVAL_ONLY_PROVIDER_IDS
LOCAL_PROVIDER_IDS = {"lmstudio", "vllm"}

BUILTIN_PROVIDERS: list[dict[str, Any]] = [
    {
        "provider_id": "deepseek",
        "display_name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "capabilities": ["chat"],
        "models_endpoint": "/models",
    },
    {
        "provider_id": "minimax-cn",
        "display_name": "MiniMax（国内）",
        "base_url": "https://api.minimax.cn/v1",
        "api_key_env": "MINIMAX_API_KEY",
        "capabilities": ["chat"],
        "models_endpoint": "",
    },
    {
        "provider_id": "lmstudio",
        "display_name": "LM Studio（本地）",
        "base_url": "http://localhost:1234/v1",
        "capabilities": ["chat", "embedding"],
        "models_endpoint": "/models",
    },
    {
        "provider_id": "vllm",
        "display_name": "vLLM（本地）",
        "base_url": "http://localhost:8000/v1",
        "capabilities": ["chat", "embedding", "rerank"],
        "models_endpoint": "/models",
    },
    {
        "provider_id": "alibaba-cn",
        "display_name": "DashScope（向量与重排）",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "embedding_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
        "rerank_base_url": "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
        "api_key_env": "DASHSCOPE_API_KEY",
        "capabilities": ["embedding", "rerank"],
        "models_endpoint": "",
        "enabled_models": [
            {"id": "text-embedding-v4", "type": "embedding", "display_name": "text-embedding-v4", "dimension": 1024},
            {"id": "qwen3-rerank", "type": "rerank", "display_name": "qwen3-rerank"},
        ],
    },
    {
        "provider_id": "siliconflow-cn",
        "display_name": "SiliconFlow（向量与重排）",
        "base_url": "https://api.siliconflow.cn/v1",
        "embedding_base_url": "https://api.siliconflow.cn/v1/embeddings",
        "rerank_base_url": "https://api.siliconflow.cn/v1/rerank",
        "api_key_env": "SILICONFLOW_API_KEY",
        "capabilities": ["embedding", "rerank"],
        "models_endpoint": "",
        "embedding_models_endpoint": "https://api.siliconflow.cn/v1/models?sub_type=embedding",
        "rerank_models_endpoint": "https://api.siliconflow.cn/v1/models?sub_type=reranker",
        "enabled_models": [
            {
                "id": "Pro/BAAI/bge-m3",
                "type": "embedding",
                "display_name": "Pro/BAAI/bge-m3",
                "dimension": 1024,
                "batch_size": 40,
            },
            {
                "id": "BAAI/bge-m3",
                "type": "embedding",
                "display_name": "BAAI/bge-m3",
                "dimension": 1024,
                "batch_size": 40,
            },
            {
                "id": "Qwen/Qwen3-Embedding-0.6B",
                "type": "embedding",
                "display_name": "Qwen/Qwen3-Embedding-0.6B",
                "dimension": 1024,
                "batch_size": 40,
            },
            {"id": "Pro/BAAI/bge-reranker-v2-m3", "type": "rerank", "display_name": "Pro/BAAI/bge-reranker-v2-m3"},
            {"id": "BAAI/bge-reranker-v2-m3", "type": "rerank", "display_name": "BAAI/bge-reranker-v2-m3"},
        ],
    },
    {
        "provider_id": "openrouter",
        "display_name": "OpenRouter（向量服务）",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "capabilities": ["embedding"],
        "embedding_base_url": "https://openrouter.ai/api/v1/embeddings",
        "models_endpoint": "",
        "embedding_models_endpoint": "https://openrouter.ai/api/v1/embeddings/models",
    },
    {
        "provider_id": "siliconflow",
        "display_name": "SiliconFlow (International)（向量服务）",
        "base_url": "https://api.siliconflow.com/v1",
        "embedding_base_url": "https://api.siliconflow.com/v1/embeddings",
        "rerank_base_url": "https://api.siliconflow.com/v1/rerank",
        "api_key_env": "SILICONFLOW_GLOBAL_API_KEY",
        "capabilities": ["embedding", "rerank"],
        "models_endpoint": "",
        "embedding_models_endpoint": "https://api.siliconflow.com/v1/models?sub_type=embedding",
        "rerank_models_endpoint": "https://api.siliconflow.com/v1/models?sub_type=reranker",
    },
]
