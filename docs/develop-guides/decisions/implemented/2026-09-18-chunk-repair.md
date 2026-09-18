# 切片边界修复与人工复核

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/chunking/mixed.py

## 问题

PDF 逐行空行和正文冒号被当作表单边界，文字虽被保留，句子却被切断。用户无法在预览中修复错误边界。

## 决策

采用上下文判定字段，正文未完句继续连接。人工边界使用审核稿 Unicode 字符位置，保存到新的不可变审核版本报告中；不复制一套可独立编辑的片段正文。界面提供相邻合并、光标拆分、定位原稿修改、恢复自动切分，保存后重新审核和预览再入库。正文修改清除旧边界和不可靠页码；仅改边界保留原稿与页码。自动方案仍使用目标长度，人工边界不随长度变化。

## 替代方案

直接编辑向量库片段会使来源和审核稿分叉，因此不采用。独立切片计划表增加第二套版本和审核状态，当前没有必要。

## 后果

人工切点会显式提示完整性仍需人工核验，超出存储长度拒绝保存；不增加自动医学改写，不自动替换现有索引。

## 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 冒号正文保持完整句 | 字段误判 | mixed.py | unit 与真实材料检查 | 逐行空行、跨行标准 | Passed |
| 人工切分持久且入库一致 | 预览与索引分叉 | review repository / mixed.py | PG、HTTP worker | 陈旧版本、非法边界、未审核 | Passed |
| 界面可修复并撤销 | 丢失草稿 | DocumentReviewPanel.vue | unit、浏览器 | 请求失败、版本切换 | Passed |


## 验证

- `docker compose exec -T api uv run --no-sync pytest test/unit -m "not slow" --show-capture=no -p no:cacheprovider`：2147 passed、53 skipped、7 subtests passed。
- `docker compose exec -T api uv run --no-sync pytest test/unit/knowledge/test_mixed_chunking.py test/integration/services/test_document_review.py -q --show-capture=no -p no:cacheprovider`：41 passed；覆盖正文跨行冒号、短字段续句、小表人工拆行表头、Unicode 边界、非法切点、超大单元人工修复、真实 PostgreSQL 版本与页码保留。
- `docker compose exec -T -e CLINEVIDENCE_SCOPE_SMOKE=1 api uv run --no-sync pytest test/integration/api/test_clinevidence_local_pdf.py -q --show-capture=no -p no:cacheprovider`：1 passed，真实 HTTP/worker/PG/Milvus，人工修订后使用 general 入口索引仍与人工预览逐片一致。Embedding 是确定性测试服务。
- 前端本地 Node 执行完整 `node --test --test-concurrency=1 "test/**/*.test.js" "test/**/*.spec.js"`：334 passed；后续光标交互补充执行 `node --test test/unit/document_review_panel.test.js` 通过。浏览器使用真实组件和合成接口验证失败重试、合并、光标拆分及定位前放弃确认；不将合成接口当成数据库证据。
- 实际八页共识重新解析生成 43 片，9 个推荐块及表格保留。逐项核对 AGO 标准、GOG 入选条件、探索问题及 SCS 条件跨行内容连续；预览产物仅保存在本地，不进入仓库。

前端 lint、build、根工程约束检查及 62 项 verifier unit、文档构建与 diff 空白检查均通过。浏览器浅色常规宽度与暗色 540 像素窄屏已核验并记录截图。
