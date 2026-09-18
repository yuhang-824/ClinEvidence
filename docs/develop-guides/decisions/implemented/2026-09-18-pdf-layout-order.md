# PDF 解析保留阅读顺序与表格字段关系

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/pdf_layout.py

## 问题

普通 PDF 提取和 RapidOCR 将识别行直接拼接，双栏正文交错，表格及表单字段关系丢失。

## 决策

在本地 PDF 与 RapidOCR 入口使用坐标重建阅读顺序，并将可识别的规则表格输出为 Markdown。文字页优先读取文字层，含图等页面由 RapidOCR 提供坐标；禁用 OCR 时对需要图像识别的页面明确失败。读取 PDF Widget 字段值，避免未写入正文流的数据遗漏。复杂布局保留现有专用解析器入口。原文不改写，不进行医学纠错；本阶段不改变切片策略、索引版本、审核 UI 或存量知识文件。

## 替代方案

- 对错序 Markdown 做正则修复：坐标已经丢失，不能可靠恢复关系。
- 强制全部文档使用重型版面服务：当前部署没有该服务，增加本地资源负担。
- 本地坐标解析：覆盖常规单/双栏、规则表格和字段行；不宣称理解任意复杂表单。

## 后果

增加轻量 pdfplumber/pdfminer 依赖，不新增模型服务。规则表格结构不明确、OCR 无坐标或有可见内容却无识别结果时显式失败。坐标仅用于解析，返回契约仍为 Markdown，尚不提供持久化区域坐标或前端原页高亮。用法和边界由 [PDF 解析参考](../../../advanced/pdf-layout-parsing.md) 维护。

## 验证

- `uv run --no-sync pytest test/unit/knowledge/test_pdf_layout.py test/unit/knowledge/test_parser_facade.py::test_pdfreader_rejects_corrupt_pdf --show-capture=no -p no:cacheprovider`：26 通过。
- `CLINEVIDENCE_SCOPE_SMOKE=1 uv run --no-sync pytest test/integration/api/test_clinevidence_local_pdf.py --show-capture=no -p no:cacheprovider`：1 通过；真实 HTTP、worker、PostgreSQL、MinIO、Milvus，回读 Markdown 确认左右栏顺序、表格及日期字段，验证检索和测试资源清理。Embedding 为确定性测试替身，不证明语义召回质量。
- 真实本地 8 页双栏文献经 service 与 RapidOCR 入口解析：8 页码、四个推荐等级、9 处推荐段落及章节顺序断言通过；合成扫描 PDF 经真实 RapidOCR 验证栏顺序和表格字段关系通过。英文 OCR 空格差异按忽略空白验证顺序，不计作逐字识别准确率。
- 负向样例覆盖坐标缺失/非法、未识别非空页、跨列框、非完整纵线、含图片时禁用 OCR、不明确的三线表；正向样例覆盖偏心双栏、独立同宽表、分段网格线、窄单元格、三列表、空字段和未渲染 Widget 值。
- `uv run --no-sync pytest test/unit -m "not slow" --show-capture=no -p no:cacheprovider -o faulthandler_timeout=90`：2093 通过、53 跳过、7 subtests 通过；随后新增表单父级循环负向样例由上述 26 项集合验证。首次运行受旧版 XLS 转换超时与进程异常影响，环境恢复后的全量通过，不将首次结果计为通过。
- `python scripts/verify_engineering_contracts.py` 通过；`python -m unittest scripts.test_verify_engineering_contracts` 62 通过；修改文件 Ruff 检查及 `git diff --check` 通过。
- 文档站点 `vitepress build docs` 通过；构建前恢复本地缺失的 Vue 依赖链接，并修正文档源码链接。新增页面相对链接检查通过。

文字层可能残缺，OCR 也可能识别错误；不能仅凭非空认定完整。倾斜、复杂合并单元格、手写表单仍需要专用版面解析和人工审核。用户文档只作本地探针，不提交仓库。
