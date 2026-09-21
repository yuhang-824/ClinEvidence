# PDF 解析引擎可选切换到 MinerU

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/parser/mineru_structure.py

## 问题

Docling 对中文期刊版式的高频误判（页眉/页脚与正文句子判为一级标题、标题行被版面切块拆碎）在真实共识文档上反复出现，人工在逐页结构审核中逐块修正成本高。用户以 mineru.net 网页与 API 对同一类文档实测后确认 MinerU 质量明显更好，要求把 PDF 解析切换到 MinerU。

## 决策

复用现有"默认 OCR 解析引擎"作为 PDF 解析引擎的选择入口：解析知识库 PDF 时用 `resolve_ocr_task_params` 解析最终引擎，`mineru_official` 时走 MinerU 官方云 API 的结构化解析，其余引擎（含自托管 `mineru_ocr`）保持本地 Docling。结构适配（`mineru_structure.build_structure_from_mineru`）以官方 API 返回的 `layout.json` 为块来源（原页坐标、阅读顺序、title 的 level、discarded_blocks），`content_list.json` 按页内顺序对齐补充表格 HTML，对不齐时降级为占位文本并加核验提示；页眉/页脚以排除文块浮出供人工确认；沿用 origin.pdf 文本层做独立覆盖检查。分发只解析引擎标识、不构造其它引擎的凭证，避免无关引擎的配置缺失挡住本地解析。不选 MinerU 时行为与既有完全一致；选 MinerU 但密钥缺失/服务不可用时显式失败，不静默回退 Docling（保证"真实病历留在内网"是显式选择而非偶然）。系统设置页的默认引擎标签与说明标注 PDF 跟随此选择、MinerU Official 会经云端。本次由用户明确要求并已在同一变更内完成验证，替代方案与风险见下，无需等待裁决的 proposal。

## 替代方案

无条件把 PDF 全部切到 MinerU：真实病历会被上传到云端，违反数据红线，不采用。新增独立的"PDF 解析引擎"配置项：与既有引擎下拉重复，管理员要多配一处且可能与图片引擎选择冲突，不采用。以 content_list 为块来源：实测其 bbox 与原页坐标存在缩放偏移，会破坏结构审核的 bbox 定位与切片来源映射，故仅用其文本。

## 后果

选 MinerU Official 的知识库上传 PDF 后走云端解析（真实病历库必须选本地引擎）；结构审核、切片、来源映射、负向校验全部沿用既有契约，前端无需改动。已入库文档不受影响，需重新解析才生成 MinerU 版结构。表格 HTML 仅在 content_list 与 para_blocks 的页内类型序列一致时按位置取用，顺序不一致时降级为占位文本由人工补齐（宁可缺内容也不张冠李戴）。原页文本层页数与解析页数不一致时跳过覆盖比对，并在页面上留全文级提示。

## 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| MinerU 输出映射为结构契约 | 字段形状不符 | mineru_structure.py | unit（真实输出形态 fixture）+ 真实端到端复验 | 表格 HTML 缺失时降级占位 | Passed |
| 标题层级按编号修正 | 层级错误 | mineru_structure.py | unit + 真实链路 | 无空格编号标题"1共识"判为一级 | Passed |
| 页眉页脚浮出为排除文块 | 静默丢失 | mineru_structure.py | unit + 真实链路 | discarded 文块不出现在审核稿 | Passed |
| 表格不对齐时不误填内容 | 张冠李戴 | mineru_structure.py | 新增 unit（类型顺序相反） | 页内类型顺序不一致仍按位置取 HTML | Passed |
| 引擎分发不破坏 Docling 路径 | 本地路径回归 | ocr_service.py | 新增分发 unit（Docling 分支被调用、MinerU 构造器未被实例化） | 非 MinerU 引擎 PDF 走 MinerU | Passed |
| 密钥缺失显式失败 | 静默降级 | mineru_official.py | 新增 unit（断言抛错且 Docling 未调用、未上传） | 未配 key 时选 MinerU | Passed |
| 本地引擎不受其它引擎凭证影响 | 失败面扩大 | ocr_service.py | 新增 unit（deepseek 凭证抛错时本地路径仍可用） | 无关引擎凭证缺失挡住 PDF 解析 | Passed |

## 验证

- `docker compose exec -T api python -m pytest test/unit/knowledge test/unit/repositories test/unit/routers test/unit/services/test_ocr_service.py test/unit/services/test_knowledge_dashboard_service.py test/unit/services/test_knowledge_folder_service.py -q -p no:cacheprovider`：512 passed。含新增 `test_mineru_structure.py` 7 项（页面尺寸、标题层级、表格 HTML 对齐、排除文块、排序与稳定 id、缺 content_list 降级、类型顺序不一致时不误填）与 `test_ocr_service.py` 4 项分发测试（选 MinerU 走适配并上传结构原件；选本地引擎调用 Docling 且不实例化 MinerU；无凭证时抛错且不回退、不上传；页数不一致留全文提示）。
- `ruff format --check` / `ruff check` / `ruff check --select I`（CI 固定版本 0.16.4，含 line-length 120 配置）对三个后端改动文件全部通过。
- `docker compose exec -T web pnpm run test:unit`：339 passed；`pnpm run lint:check`、`pnpm run build`：通过。
- 负向案例：`build_structure_from_mineru(LAYOUT, [])` 断言表格降级为占位文本并加核验提示——保证对齐失败不被静默当成正确内容。
- 真实链路验证一（Docker 全链路 + mineru.net 云端，UI 路径）：合成中文 PDF（标题/正文/页眉/页脚）经上传对话框选择 MinerU Official 上传，云解析约 26 秒完成后结构审核可用，数据库核验：标题层级 1/2、页眉页脚为排除文块（note="解析器判定为页眉/页脚或噪声，请人工确认排除"）、独立文本层覆盖检查生效（"疑似缺少 5/110 个字符"来自 origin.pdf 比对）。
- 真实链路验证二（重构后复验，直接调用 parse_knowledge_document）：合成中文 PDF 经 MinIO → MinerU 云端：`parser=mineru`、`parser_version=3.4.4`、标题层级 1/2、页眉与页码为排除文块、覆盖检查提示"疑似缺少 4/68 个字符"、结构原件上传成功，验证对象与临时 KB 已清理。
- 前端文案更新为"OCR 解析引擎（PDF 默认走 Docling；选 MinerU Official 时 PDF 走 MinerU）"。
- 提交前自查与独立审查修正：移除无消费者的 `_last_zip_path` 与 `_parse_bbox` 死代码；PDF 分发改为只解析引擎标识、仅在 MinerU 路径构造该引擎凭证；MinerU 未返回页面结构时显式失败；适配器在缺 `page_idx` 时按页序回退编号、并在构建页时保留页引用替代逐块线性查找；表格对齐收紧为类型序列一致才启用。

## 未解决

- 自托管 `mineru_ocr` 引擎的 PDF 结构化路径未接（其 `mineru.py` 仅返回 markdown），离线场景暂用 Docling；需要时按同一适配层接入。
- 表格 HTML 的顺序对齐在异常版式（跨页表、分栏嵌套）可能失配并降级占位，靠结构审核人工补齐。
- 后端全量回归 `test/unit -m "not slow"` 含已知的 `test_run_worker.py` 本机卡点（见 2026-09-19 决策记录）：本次在知识库/仓储/路由/相关服务范围内运行，`test_run_worker.py` 未包含；该文件单独运行通过（50 项 15 秒，见 2026-09-19 记录）。
- 仓库既有的后端 lint 漂移已在后续变更中清理（CI 的 ruff 作业只检查 `package`，此前在 main 上是红的）：`ruff check package` 的 1 处 E501（`knowledge/implementations/milvus.py`）、`ruff format package --check` 的 3 个文件（`milvus.py`、`services/chat_service.py`、`services/document_review_service.py`）与 `ruff check --select I package` 的 11 处导入顺序全部修好，三条门禁现已通过。`test/` 与 `server/` 下另有 23 个文件会被 `ruff format --check` 重排、24 条 lint 报错，不在 CI 范围内（其 ruff 命令也只覆盖 `package`），未处理。
