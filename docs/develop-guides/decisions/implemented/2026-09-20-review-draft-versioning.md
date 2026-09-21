# 审核版本按审核轮次冻结

状态：implemented
类型：simplification
Owner：backend/package/yuxi/repositories/document_review_repository.py

## 问题

逐页结构审核中每次"保存结构修订"都会生成一个新审核版本，一份 11 页文档审核完成产生 25 个版本：版本列表被同一次审核的中间态淹没，且前端以版本号为编辑器重建 key，每次保存后视图跳回第 1 页，审核者必须手动翻回未核验的页。

## 决策

版本语义改为"一次审核循环一个版本"：未审核且未入库的最新版本是当前草稿，保存（结构修订、清洗稿正文、人工切点）原地更新该草稿，不产生新版本；点击"审核通过"才冻结为已审核版本；已审核或已入库的版本不可变，再次修订生成新草稿版本。并发保护由版本号比对改为草稿时间戳比对：保存请求携带加载草稿时的 `created_at`，服务端不一致即拒绝（"草稿已被其他修订保存"），持锁串行下恰好一方成功，并发修订仅一方成功的既有边界保留。保存成功后前端定位下一张未完成核验的页（环形查找），编辑器不再因版本变化重建。原地更新使编辑器组件在保存后不会重挂，因此编辑器的"未保存"标记必须在保存成功后重新基准（`rebaseline`，用响应式的 `savedSnapshot` 而非普通变量，否则 `dirty` 计算属性不会失效），否则保存按钮在保存后一直保持可点。不做独立的"意图识别/提案"阶段：由用户明确提出、同一变更内完成实现与全部验证，无待裁决的替代风险。

## 替代方案

保留每次保存一个版本、仅前端合并显示：版本表仍被中间态淹没，与用户诉求相反，不采用。审批时生成新版本而非原地冻结标记：会改变 approved_revision 的版本定位且多一次行迁移，现有"标记 approved_at"方案已满足，不采用。去掉并发时间戳守卫、接受同版本后写覆盖：会静默丢失先保存者的修订，不采用。

## 后果

一份文档审核一轮只产生一个版本，版本列表按审核轮次增长；预览切片的版本引用在草稿保存后仍然有效（版本号不变），减少"版本已更新"式重试。审核过程中的逐次保存不再各自留痕，审计粒度从"每次保存"变为"每轮审核"（页面级核验人/时间与文块说明仍保留在结构 JSON 中）。已入库版本永不被原地修改（索引前置条件要求先审核），旧检索片段的来源版本不受影响。

## 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 草稿保存不产生新版本 | 版本爆炸 | document_review_repository.py | 集成测试断言版本列表不变 | 陈旧时间戳外他人保存 | Passed |
| 并发草稿保存仅一方成功 | 静默覆盖 | document_review_repository.py | 集成测试（时间戳守卫） | 双方同基保存仍双双成功 | Passed |
| 已审核/已入库版本不可变 | 冻结失效 | document_review_repository.py | 集成测试断言新草稿版本 | 已审核版本被原地改写 | Passed |
| 保存后定位下一张未核验页 | 手动翻页 | StructuredDocumentReview.vue | 编辑器 unit（跳过已核验/环形） | 全核验后仍跳页 | Passed |
| 原地保存后"未保存"标记复位 | 保存按钮恒亮 | StructuredDocumentReview.vue | 编辑器 unit（保存后 `dirty` 为假） | 保存成功后仍提示有未保存改动 | Passed |

## 验证

- `docker compose exec -T api python -m pytest test/integration/services/test_document_review.py -q -p no:cacheprovider`：4 passed（真实 PostgreSQL、隔离 Schema）。既有两个测试按新语义重写：结构保存原地更新后审核版本为 1；并发保存由 `base_saved_at` 时间戳守卫拒绝陈旧方；`test_boundary_revision_preserves_pages_resets_approval_and_automatic` 的草稿内保存不再产生新版本。
- `docker compose exec -T api python -m pytest test/unit/knowledge test/unit/repositories test/unit/routers test/unit/services/test_ocr_service.py test/integration/services/test_document_review.py -q -p no:cacheprovider`：526 passed（该命令范围含本次新增的请求体契约用例）。前端 `pnpm run test:unit`：360 passed；`lint:check`、`build`：通过。新增编辑器 unit `保存跳页：定位下一张未核验的页，支持环形回绕`。
- 接口契约：`DocumentReviewInput` 新增可选 `base_saved_at`，服务层与仓储透传；不传时跳过时间戳比对（兼容既有直调方），版本比对仍然生效。字段名必须与前端发出的键名一致：pydantic 默认忽略未知键，前端曾用 `baseSavedAt` 发送，模型解析后 `base_saved_at` 恒为 `None`，HTTP 路径上的时间戳守卫实际是死的（集成测试直调服务层、HTTP 用例 skip，没有一层能发现）。前端已改为发 snake_case，并用 `test/unit/routers/test_document_review_input.py` 固定两侧契约：字段名集合、`base_saved_at` 能到达模型、camelCase 键被静默丢弃——最后一条是负向案例，说明这类错误为什么必须靠断言而不是靠报错发现。
- 面板侧：`DocumentReviewPanel.vue` 在保存成功并 `nextTick` 后调用编辑器的 `rebaseline()`；面板单测按原地更新语义 mock 保存结果，并断言请求携带的 `base_saved_at` 等于该版本真实的 `created_at`（此前断言的是 `record.revisions.at(-1).created_at` 与 mock 都未定义，等于没有断言）。`rebaseline` 的接线由 `审核通过前置校验拦截未核验结构并显示具体原因，通过后放行` 用例固定：该用例的 fixture 含 `report.structure`（编辑器才会挂载），通过编辑器发出的 `save` 事件进入保存路径并断言基线已复位——删掉那行调用该用例失败。
- 旧能力不存在：每次保存都生成新审核版本的语义与相应版本列表行为已取消；保存不再使预览切片的版本引用失效；保存成功后不再残留"有未保存改动"的提示。
- 重新引入条件：只有出现「草稿被并发修订覆盖且时间戳守卫未能拦住」的真实案例，并提供对应负向证据时才收紧并发保护；收紧方式为在 `document_review_repository.py` 的草稿更新分支加强受锁校验，不恢复每次保存一版。

## 未解决

- 已有文档的历史版本（如田东立的 25 个版本）按原样保留，不做合并清理；新语义只影响之后的保存。
- 检索测试页参数快照导致修改检索配置后需刷新页面的问题与本次无关，未处理。
