# PDF 结构化解析与逐页审核

状态：implemented
类型：feature
Owner：backend/package/yuxi/services/document_review_service.py

## 问题

复杂 PDF 的文字数量和转换成功状态不能证明阅读顺序、表格单元格或流程分支正确。Markdown 单独持久化还会丢失图片子节点、坐标和原始结构，人工改稿后难以定位来源。

## 决策

知识库 PDF 使用本地 Docling 标准流程，保存原始 JSON、逐页来源坐标和可修订文块。原页、原始文块和审核稿同屏对照；逐页核验阅读顺序、完整性和对应关系。用户可调整文块顺序、正文和类型，复杂关系必须记录核验说明。修改生成新版本并失效审核。审批及索引提交边界都检查最新版本的逐页核验，不把前端按钮作为边界。Mixed 使用结构来源信息，标题作为正文上下文，表格和关系单元保持完整。原始数据和历史已发布版本不被覆盖。

本功能保证证据保留、显式审核和版本隔离，不承诺任意 PDF 无需人工就能准确理解。审核记录是人工判断，不是机器准确率证明。

## 替代方案

直接入库 Docling 默认 Markdown 会遗漏图片内部文本；继续扩展 PDF 坐标启发式难以覆盖表格与流程图。自动调用视觉模型补写关系尚无经验证的语义正确性，不纳入本次范围。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 原始 JSON、图片子文本和页码保留 | Markdown 静默遗漏 | parser / document review service | 单元测试、真实样本解析 | 图片子节点缺失、空页 | 通过，证据见下文 |
| 文块修订、排序和逐页审核持久化 | 修改仍沿用批准状态 | document review repository | PostgreSQL / HTTP integration | 陈旧版本、未核验页、伪造块 ID | 通过，证据见下文 |
| 未审核内容不能索引 | 直接调用绕过 UI | knowledge file repository | HTTP / worker 链路 | 未批准、修改后重用旧批准 | 通过，证据见下文 |
| 标题和关系单元来源可回读 | 标题碎片、表格切断 | Mixed | unit / 片段来源断言 | 多层标题、跨页内容、长关系单元 | 通过，证据见下文 |
| 原页审核可操作 | 只有接口无界面 | DocumentReviewPanel | web unit / build / 真实浏览器 | 保存失败、历史只读、页面切换 | 通过，证据见下文 |

## 后果

Docling 增加本地模型和运行时依赖，部署需要离线预取。结构检查只能提示疑点，复杂表格和流程关系仍需逐页审核。完整源结构与修订增加存储量；原始 JSON 使用不可变对象，数据库保存审核版本和紧凑文块。

## 部署与操作

运行时固定 Docling 2.128.0、PyMuPDF 1.28.2，PyTorch 使用 CPU 软件源。PDF 知识库入口固定使用 Docling；通用附件和单独图片仍由原解析入口处理。Docling 使用本地版面模型、准确表格模式和中文 RapidOCR 识别器（含拉丁文字），每进程限制为一次解析、两条 CPU 线程。

在可联网的构建环境先运行 `docling-tools models download layout tableformer rapidocr --rapidocr-backend-lang onnxruntime:ch -o <模型目录>`，将目录完整拷入内网。设置 `YUXI_DOCLING_MODEL_DIR` 为宿主机目录（默认 `docker/volumes/docling-models`），Compose 将其只读挂载至 API 和 worker。运行时显式指定 artifacts_path；缺失模型报错，不自动联网下载或切换解析器。模型权重不提交到 Git。

新上传或重新解析的 PDF 生成结构审核版本，历史已入库版本保持可读。打开文档清洗审核页，对照原页检查每个文块，必要时调整类型、标题层级和顺序，修订表格 Markdown 或补录遗漏文块。错误文块可以保留原件并注明“不参与检索”；涉及表格、图示、排除或异常时填写本页核验说明。保存并逐页核验后，预览 Mixed 片段、批准最新版本，再入库。新修订不会继承批准状态；结构修订清除旧人工切点。

独立 PDF 文本层字符覆盖检查只提示疑似遗漏：不能检查扫描图像中的漏字，不能证明阅读顺序、否定关系或箭头语义。无文本层的页面显式提示逐字核对。流程关系文块按整体切片，表格保留行与重复表头；超出存储上限会拒绝入库，要求先在结构稿中人工分段。片段保存审核版本、字符来源、页码和文块坐标。

跨页文字按 Docling 字符来源拆成稳定子文块；越界、重叠或非空文本未覆盖时拒绝解析，不将全文错归到第一页。同页多区域保存全部 provenance 与完整正文，显示联合范围并要求核对块内顺序。当前多来源表格、图示无法可靠分配字段归属时拒绝解析，须先按页拆分 PDF 再逐页审核；拆分本身不能保证跨页表格、流程的关系完整，审核人仍需在关系文块中补全必要上下文。

## 验证

- 2026-09-18：后端 `pytest test/unit -m "not slow"` 单进程执行在服务测试交界处挂起，未通过整体门禁；拆为 `test/unit/services` 与 `test/unit --ignore=test/unit/services` 完整执行，分别 737 与 1432 项通过，53 项跳过。队列 9 项和 worker 50 项独立执行通过。未修改无关的队列实现，不把分组通过等同单进程全量通过。
- `pytest test/integration/services/test_document_review.py`：真实 PostgreSQL 审核版本、来源与批准约束通过；相关解析、结构审核、Mixed 和源文件单元测试通过。
- `pytest test/integration/api/test_structured_document_review.py`：真实 HTTP、Docling worker、PostgreSQL、MinIO、LM Studio Embedding 与 Milvus 链路 1 项通过；验证未核验审批拒绝、伪造来源拒绝、旧版本冲突、版本 2 批准及索引片段来源。默认线上 Embedding 的本机凭据不可用，验证时仅以 TEST_EMBEDDING_MODEL 指定本地测试模型，未改用户供应商配置。
- 三份既有 Docling JSON 共 254 页完成来源投影检查；实际离线重跑 4 页复杂样本，产生 48 文块、24 个 Mixed 片段，审核稿非空白字符被切片覆盖。该检查不等于 254 页人工语义验收，临床内容仍需逐页审核。
- Web 全套单元测试 335 项通过；随后新增二进制响应适配用例，API 边界文件 13 项通过，审核组件 2 项通过。lint、生产构建通过。真实浏览器验证原页图片、修订、保存版本 2、三项核验、表格片段预览与切片修复定位；检查了窄屏和桌面两列布局。浏览器验证发现并修复 Response 未转 Blob 的问题。
- 工程约束校验及其 62 项单元测试通过；文档构建通过。独立 reviewer 指出的跨页来源归属、人工切点拆标题、结构稿定位三个问题已修复，并补充负向测试。

本决策替代旧依赖测试中禁止 Torch 运行时的假设：Docling 标准版面与表格模型需要 Torch；锁文件仍禁止完整 docling 元包及 NVIDIA/CUDA、Triton 依赖，Torch 和 torchvision 仅使用 CPU 源。本次本机已安装依赖和离线模型并启动服务，未以全新镜像构建验证部署；部署配置和锁文件是后续镜像构建入口。
