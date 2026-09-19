# 标题只作为正文上下文

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/chunking/mixed.py

## 问题

自动切片先为每个标题生成一个独立片段，再在收尾阶段删除已被正文携带的标题，同时保留“孤立标题”。当标题后面直接跟同级标题，或标题位于文末时，它就作为只有标题文字的片段进入向量库。这类片段命中后没有可回答的正文，占用召回名额，与同章节正文片段重复。复核 53 片真实共识材料时未出现此类片段，但生成路径始终存在，不满足“标题不能单独存在”的入库要求。

## 决策

标题只作为后续正文的上下文，不产生片段。正文片段携带从顶层到当前层的全部标题路径，标题文字保留在 `content` 与 `source_metadata.section`，供检索与来源回读。没有任何正文的标题（同级标题相邻、文末标题）不进索引；纯标题文档仍按整篇生成一个正文片段，保证内容不静默丢失。层级继续取自文档的 Markdown 层级标记，并保留 1–6 级裁剪规则。

## 替代方案

保留孤立标题片段用于标题命中检索：标题单片的检索价值低于同章节正文片段，且重复占用向量，不采用。按标题编号（1、1.1、1.1.1）推断层级代替 Markdown 层级：文档编号风格不统一（年份、附录、无编号标题混杂），推断错误会把同级标题当成上级标题，使路径错误，风险高于收益，当前不采用；层级错误由逐页结构审核与清洗稿修正。前端 `章节标题` 标签保留，用于显示旧索引中已存在的历史片段。

## 后果

正文片段都带上级标题路径，标题不再单独占用向量；没有正文的标题只保留在审核稿中，不进入索引，检索不到。已入库的旧标题片段不会自动重写，需重新审核并入库才生效。运行中的 api 与 worker 通过源码挂载立即生效，无需重建镜像。

## 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 标题不单独成片 | 孤立标题进索引 | mixed.py | 同章节文件 unit | 同级标题相邻、文末标题 | Passed |
| 正文片段带上级标题路径 | 小标题丢失上级 | mixed.py | unit 与真实材料重跑 | 三级嵌套标题（改动前已满足，本次固化为回归 guard） | Passed |
| 自动切片无标题类型片段 | 标题类型片段回归 | mixed.py | unit 断言 kind | 文末孤立标题，旧实现产出 heading 片段 | Passed |
| 纯标题文档不丢内容 | 全文被丢弃 | mixed.py | unit 兜底断言 | 仅一行标题，旧实现产出 heading 片段 | Passed |

## 验证

- `docker compose exec -T api python -m pytest test/unit/knowledge/test_mixed_chunking.py -q -p no:cacheprovider`：41 passed，含 3 项新增：`test_nested_headings_travel_with_body_as_full_path`、`test_heading_never_becomes_standalone_chunk`、`test_automatic_chunks_are_never_heading_kind`。
- 负向案例在旧实现上失败：临时回退 `mixed.py` 后 `test_heading_never_becomes_standalone_chunk` 因 `assert 2 == 1` 失败（旧实现为 `# 概述` 生成独立片段），`test_automatic_chunks_are_never_heading_kind` 因文末孤立标题生成 heading 片段而 `assert 3 == 2` 失败；恢复后两项通过（恢复后 `git diff --stat` 与本记录描述一致）。
- `docker compose exec -T api python -m pytest test/unit/knowledge -q -p no:cacheprovider`：283 passed。
- `docker compose exec -T api python -m pytest test/integration/services/test_document_review.py test/integration/api/test_structured_document_review.py -q -p no:cacheprovider`：4 passed、1 skipped，覆盖真实 PostgreSQL 版本、审核与结构修订。
- 真实材料重跑：`kb_zxyhv50fzr` 的 `file_7eb481` 审核版本 1 重新切片得到 53 片（正文 42、推荐 9、表单 1、表格 1），孤立标题 0 片，`1.1 确定临床问题` 片段路径为 `['1 共识制定方法及流程', '1.1 确定临床问题']`；改动前后片段 id 集合与类型分布一致。

## 未解决

复核该材料时发现 docling 把页脚（`肿瘤学杂志 2025 年第 31 卷第 2 期`）和一个正文句子（`卵巢癌是女性癌症死亡的第五大原因，据估计，`）导出为一级标题，使参考文献片段的路径被页脚顶替。该问题属于解析与逐页结构审核范围（阶段计划 [M3](../../clinevidence-roadmap.md)），不在本次切片改动内。

人工切点路径（`revision.report.chunk_boundaries`）仍按人工边界切分，`validate_boundaries` 只要求每段非空；人工把切点放在标题行之后仍会产生只有标题的片段。人工边界是审核者的显式决定，本次不为它增加自动改写；若需要禁止，应在预览与保存处提示而不是静默合并。

后端全量回归 `test/unit -m "not slow"` 在本机未跑完：套件推进到 `test/unit/services/test_run_worker.py`（约 78%）后停止推进十余分钟；该文件单独运行 50 项 15 秒通过，与相邻的 `test/unit/services/test_run_queue_service.py` 同跑 59 项 18 秒通过，`test/unit/services` 单独运行同样停在同处。表现为既有测试隔离问题，与本次切片改动无关，未在本记录内宣称全量通过。
