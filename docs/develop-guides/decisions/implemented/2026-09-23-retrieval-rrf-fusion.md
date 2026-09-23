# 检索融合改为应用层 RRF,患者病历检索升级为混合检索

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/rank_fusion.py

其余语义 Owner 在决策正文中分工：文档知识库检索分支与门控在 backend/package/yuxi/knowledge/implementations/milvus.py，患者 collection schema 与双路检索在 backend/package/yuxi/knowledge/patient_index.py，前端排序口径在 web/src/utils/kbChunkScore.js。

## 问题

文档知识库 hybrid 模式用 Milvus `WeightedRanker` 做加权分数融合：稠密余弦相似度聚集在 0-1 窄区间而 BM25 分数无上界，两路量纲不可比，名义权重（默认 0.7/0.3）与真实排序贡献脱节，且没有权重调优的评测闭环。融合分随后被当作相似度用于 `similarity_threshold` 门控和展示，语义脆弱——融合策略一旦更换（如 RRF），同一门控会把全部结果过滤光。

患者病历检索为单路纯向量：医生查询里的药品名、检查名等精确词面只有向量近邻一条路，缺 BM25 词面精确匹配能力；患者 collection schema 也没有文本字段，无法启用 Milvus 内置 BM25。

## 决策

融合公式采用 RRF（倒数排名，k=60），在应用层实现：两路各自 `collection.search`，按各路名次融合，实现收敛在 `rank_fusion.reciprocal_rank_fusion`，文档知识库 hybrid 模式与患者检索共用。

- 门控：`similarity_threshold` 只作用于向量路的余弦相似度（融合前），保留"相似度过低的证据不采信"的可解释门控；BM25 路分数无量纲，不设绝对门控。仅 BM25 命中的文块不携带 `score`，不伪造相似度。
- 展示口径：`score` 字段在所有模式下保持相似度语义；排序共识分放 `rrf_score`，BM25 原始分放 `bm25_score`，经 `build_search_output` 与 summary 检索预览透传。前端排序依据统一为重排分 > 融合分 > 相似度（`kbChunkScore.js` 单点判定），患者来源分组排序与"按相似度降序"标注同步修正，避免把融合分当相似度百分比展示。
- 患者混合检索：collection schema 增加 `content`（VARCHAR，中文 analyzer，仅服务于 BM25 稀疏投影；内容真值仍在 PostgreSQL）与 `content_sparse` 稀疏字段、BM25 function 与稀疏索引；写入时随 records 提供 `content`；检索为同 filter 表达式下的双路 search + RRF 融合，命中仍必须回读 PG 并校验快照成员后才能引用。
- 旧 schema 的患者 collection 首次访问时重建（与文档知识库既有策略一致），重建后检索为空，需重新执行索引写入恢复；`score` 字段与快照校验链路不变。

## 替代方案

Milvus 原生 `RRFRanker`（`hybrid_search` 内融合）：改动最小，但 `hybrid_search` 只返回融合分，拿不回各路分数——阈值门控只能整体丢弃，前端相似度口径会被 0.03 量级的 RRF 分打穿，需要改动所有展示点且失去门控；放弃。

保留 `WeightedRanker` 并调权重：需要先做分数校准和权重调优闭环，当前没有该评测支撑，名义权重与真实贡献脱节的问题依旧；放弃。

患者链路维持单路向量：缺 BM25 词面精确匹配，对含药品名、编号的查询召回不足；放弃。

## 后果

每次混合检索是两次 Milvus search 而非一次 `hybrid_search`（总体延迟近似，融合逻辑变为可离线单测的纯函数）。`vector_weight`/`bm25_weight` 从知识库查询参数中移除，存量知识库保存的旧值被忽略。已有部署的患者 collection 升级后首次访问会被重建，重建后检索返回空——这是显式可观察的空结果而非静默降级，需重新发布/重新索引才恢复向量投影。RRF 丢弃分数的置信度信息：绝对质量判断由向量路门控（有相似度的文块）与 reranker 精排承担，`rrf_score` 只表达多路共识强度，任何展示不得把它标注为相似度。

## 验证

单元测试：`test/unit/knowledge/test_rank_fusion.py`（融合公式、名次起点、k 语义、k 非法值拒绝）、`test/unit/knowledge/test_patient_index.py`（schema 含 BM25 投影、旧 schema 判定不支持、融合保相似度口径且仅 BM25 命中不伪造 score、融合条数截断回 top_k）、`test/unit/plugins/test_milvus_kb.py`（hybrid 双路各自检索 + RRF 顺序、门控只作用于向量路的负向用例、权重配置不再出现）、`test/unit/services/test_clinical_retrieval.py`（`query_text` 透传给患者检索）44 项全部通过；`docker compose exec -T api uv run --no-sync pytest test/unit -m "not slow"` 2241 通过 / 53 跳过 / 1 失败，唯一失败 `test_context_auth.py::test_normalize_agent_context_config_expands_null_and_filters_explicit_lists` 在不含本改动的干净树上同样失败（`git stash` 后复验），与本改动无关；`python scripts/verify_engineering_contracts.py` 通过；`python -m unittest scripts.test_verify_engineering_contracts` 69 项中 68 项通过，唯一失败是 Windows 环境无符号链接特权的用例搭建错误，干净树上同样失败。前端：web 容器内 `pnpm run lint:check` 通过；messageProcessor/messageGrouping/messageDebug 定点单测 48 项全部通过（覆盖 `rrf_score`/`bm25_score` 透传链路）；web 全量单测中 3 个失败均位于另一批在途改动的文件（structured_review、项目会话分组），与本改动文件无交集。真实 Milvus 实例上的 schema 创建、BM25 function 与双路检索语义未在本次环境执行，部署验证按测试规范需补 integration/E2E：升级后患者 collection 重建与重新发布的恢复路径、hybrid 检索真实返回与前端相似度/融合分展示。
