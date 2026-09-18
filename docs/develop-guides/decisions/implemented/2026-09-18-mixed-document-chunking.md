# 混合材料切片与来源追溯

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/chunking/mixed.py

## 问题

文献、表单、表格和英文材料共存时，长度切分可能拆散表头与行、字段与值、推荐及其条件；片段还需要定位到实际入库的审核版本。

## 决策

字段识别与人工修复由[切片边界修复](2026-09-18-chunk-repair.md)补充；仅验证字符覆盖不构成完整语义验证。

新增显式 mixed 策略，清洗审核页提供该策略的预览入口。Markdown 章节作为上下文，长表按完整行拆分并重复表名、表头，字段组与推荐意见保持完整；英文普通正文按句切分，逐行空行正文按预算组合，未完句不硬拆。超长完整单元显示提示，超出存储上限时拒绝并要求人工分段。数字行不推断为标题，避免将剂量或时长错误附加到后续片段；章节不明确时由审核者补充 Markdown 标记。

清洗报告拥有可靠解析页边界；人工修订版本不继承 PDF 页码，但保留审核稿字符和行位置。KnowledgeChunk.source_metadata 保存结构、版本、正文和重复上下文的位置，storage-migrator 幂等升级知识 Schema 到 4，历史片段保持 nullable 来源。Milvus 返回结果经 PostgreSQL 核对归属及内容后附加来源信息。

预览与索引使用同一个切片器。文件 repository 在索引 claim 的文件锁内验证审核与预览版本，拒绝陈旧请求。片段标识包含内容和来源范围哈希，来源接口同时校验知识库、文件和期望版本，重新入库不会把新来源显示在旧卡片下。查询只需读取权限；索引继续要求管理权限。编辑和审核不自动更新索引。

## 替代方案

全库语义聚类不能保证字段关系；大模型重写片段增加医疗数值变更风险及外部模型依赖。裸数字章节推断无法可靠区别剂量，采用显式标题更保守。通用策略继续供既有知识库使用，不自动重建存量索引。

## 后果

原段落追溯定位到不可变审核稿，PDF 页码只在已知时显示，不提供坐标高亮。结构保护依赖解析稿中的标签和段落证据，复杂合并单元格、无标签表单和识别错误需先修订。目标长度是近似 token 数，不保证所有完整单元适配任意嵌入模型。重新入库沿用现有替换流程，不提供原子版本切换。跨语言模型效果、召回与重排评测、最终回答引用高亮属于后续工作。

## 验证

- `docker compose exec -T api uv run --no-sync pytest test/unit -m "not slow" --show-capture=no -p no:cacheprovider`：Passed，2133 项通过、53 项跳过，7 个 subtests 通过。mixed 专项 24 项覆盖长表、表名、字段数值、推荐条件、英文缩写与小数、裸数字误判、过长结构块及来源标识。
- `docker compose exec -T api uv run --no-sync pytest test/integration/services/test_document_review.py -q --show-capture=no -p no:cacheprovider`：Passed，真实 PostgreSQL 2 项覆盖幂等升级保留原数据、陈旧预览拒绝、并发审核边界、重新入库后的旧版本来源请求及跨库隔离。
- `docker compose exec -T -e CLINEVIDENCE_SCOPE_SMOKE=1 api uv run --no-sync pytest test/integration/api/test_clinevidence_local_pdf.py -q --show-capture=no -p no:cacheprovider`：Passed，真实 HTTP、worker、PostgreSQL、MinIO、Milvus 链路验证预览与入库片段一致、来源回读、检索 metadata 和新稿不污染旧来源。Embedding 为确定性测试服务，不代表真实语义质量。
- 前端在已安装依赖的本地 Node 运行 `node --test --test-concurrency=1 "test/**/*.test.js" "test/**/*.spec.js"`、`node node_modules/eslint/bin/eslint.js . --max-warnings=0`、`node node_modules/vite/bin/vite.js build`：Passed，334 项测试通过。浏览器合成材料验证预览、来源展开、浅色、暗色及 540/1366 像素宽度，截图已记录；临时验证页面已清理。
- `python scripts/verify_engineering_contracts.py`、`python -m unittest scripts.test_verify_engineering_contracts`：Passed，62 项测试通过。文档目录执行 `node node_modules/vitepress/bin/vitepress.js build` 和 `git diff --check`：Passed。
- 用户提供的八页共识在本地解析后按默认长度生成 99 个片段，9 个推荐块及全部表格行保留；非空白字符均被来源范围覆盖。8 个片段因完整单元超过目标长度产生提示。测试材料与预览产物不进入仓库。
