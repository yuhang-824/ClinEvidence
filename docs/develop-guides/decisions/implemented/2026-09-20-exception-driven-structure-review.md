# 逐页结构审核改为异常驱动

状态：implemented
类型：feature
Owner：backend/package/yuxi/knowledge/structure.py

MinerU 图示的视觉草稿生成由[视觉流程图解析](./2026-09-23-mineru-flowchart-vision.md)补充；本记录中的图示占位与人工审核要求仍是失败和批准边界。

## 问题

105 页期刊指南按原契约需要人工逐页核验：每页勾选三项核验并填写说明，且"页面存在任何被排除文块"就强制页面说明。解析器把页眉（期刊名、页码）浮出为排除文块，于是几乎每一页都被强制要求写说明，人工成本与页数线性增长。真正需要人判断的只有含表格、图示或内容缺失的少数页。

独立文本层比对本应分担这个判断，但原实现按字符出现次数做多重集相减：期刊刊名、表头、脚注序号在页上重复出现，解析结果只保留一次，重复部分被当成"疑似缺失"，94 个可比对页里 90 个都带上"需核对"条目。该口径下覆盖中位数 96.7%、5% 分位 77%，按比例定阈值无法把期刊页与表格页分开。

## 决策

审核改为异常驱动，规则集中在 `structure.py`：`mark_auto_review` 解析时判定并写入页上 `auto_review = {rule, parser, parser_version, at}`。

- `blank-page/v1`：该页无文本层（比对实测 `characters == 0`）、无图像与矢量图形（`has_visual_content` 为假）、没有正文文块、且没有需要人工决策的解析异常。四个条件缺一不可——只看"无文本层且无正文"会把 OCR 整体失败的扫描页当成空白页静默放行；不看异常会把解析器同时报告结构可疑的页一并放行。`has_visual_content` 必须是显式的 `False`，缺证据时不判空白。
- `no-anomaly/v1`：存在独立文本层比对记录且覆盖达标，没有需要人工决策的异常，且无表格或图示文块。解析器自动排除的页眉页脚不产生异常，因此期刊页命中此规则。
- 其余页保持人工核验：含表格、图示，解析器报告异常（缺文本层与无正文块之外的项，如标题疑似续句、孤立文字、多来源、表格未提取；空块不再产生页面级 issue），或人工介入过。

`NON_BLOCKING_ISSUES` 列出解析器对缺失文本层与无正文文块的固有判断（`无可用 PDF 文本层`、`页面没有正文文块`）：它们本身不表示需要人工决策，其余 issue 都强制人工。空结构块既不在此列，也不再产生页面级 issue：解析器对没取到文字的区域直接写入排除与文块说明，若同时再挂一条"存在空结构块，请对照原文检查"，规则要求的"无异常"与解析器必然报的异常会互相打架，同一页既被机器核验、又显示一条待人工条目（第 11 页实际出现过这种自相矛盾）。"不强制人工"也不等于"可以进索引"——占位正文不是内容：解析器对没取到文字的区域直接写入 `excluded: True` 与说明，逐页审核里显示为"不参与检索"，人工确认原页确有内容时补充原文即可取消排除，`revise_structure` 与前端校验都拒绝让仍是占位文本的文块参与检索。表格与图示的占位不自动排除：那两类页本来就必须人工补充或排除，自动排除会让表格内容静默消失。

判定为空白页的页连同臆造的占位文块一起清理：MinerU 在无内容页上仍会给出覆盖整页（bbox 等于页框）的空结构块，那是版面模型的假区域，保留它会让"不参与检索 + 原页如有内容请补充"看起来像内容被丢弃。空白页要求无文本层、无图像与图形、无正文文块且无结构异常，四个条件齐全才清理该页全部文块，人工仍可用"补录遗漏文块"补内容。`page_has_visual_content` 除 `get_images()`/`get_drawings()` 外还检查文本页的图片块，避免嵌套在 Form XObject 里的扫描图被漏判成空白。

`check_native_coverage` 按"本页文本层里有没有解析结果完全找不到的连续片段"比对，查找范围是**整份解析结果**而不是本页文块：MinerU 会把跨页续排的段落整体归到起始页，只看本页会把续排内容误报成缺失（实测 105 页文档里有 22 页属于这种误报）。两侧都做 NFKC 兼容归一、去掉表格文块的 HTML 标签、只比较字母数字，并按标点把文本层切成片段——个别字符的归一化差异只影响它所在的片段，不会让整行判为丢失。缺失片段本身作为样例写进证据（`native_text_check = {characters, absent_characters, absent_sample, coverage, has_visual_content}`），"需核对"条目直接给出原文片段，不需要人工再从字符集里猜。`coverage < AUTO_REVIEW_MIN_COVERAGE`（0.99，即容忍 1%）才追加条目；判据由 `coverage_shortfall` 一处拥有，生产者和规则共用，规则自身也独立校验比例，未来新增解析器漏写 issue 时不会放行。

闸门 `require_structure_review` 跳过带 `auto_review` 的页，不要求三项核验与人工签核。文件级 `approved_by`/`approved_at` 仍是人工动作，因此页级证据分机器与人工两种来源，文档仍有人负责。三处执行点（审批分支、`approved_revision`、索引写入的行锁校验）共用该函数，行为自动一致。

`revise_structure` 只对被人动过的页丢弃 `auto_review`：页未被动过等价于该页文块标识序列与存储一致、每个文块的 `(text, note, kind, level, excluded)` 与存储相同（比较使用与解析产出相同的默认值，缺字段不等于被改），且页面说明未被改写。页面说明是人工对该页的判断，写说明即接管核验，审阅者据此可以在产品内推翻机器结论。机器核验页的 `checks` 保持解析时的 false，所以保存动作不会把机器核验页批量盖章成人审。

前端 `StructuredDocumentReview.vue` 同步镜像：页标签区分"机器核验/已核验"、审批校验与保存后跳页**整页**跳过机器核验页（含说明要求）、工具栏显示"机器核验 N 页 · 待人工 M 页"、页上展示覆盖与缺字证据（只按去重口径展示数字，旧记录不套用同一标签），`changed()` 与页面说明改动都清除机器核验。"机器核验"计数指当前仍由机器核验的页，人工勾选接管后该页转入"已核验"，因此两个数字不保证等于总页数。保存动作放在本页核验项与说明下方而不是顶部工具栏：审核动线在看文块、勾核验、写说明处结束，保存不需要回到顶部。

不做既有版本回填：旧 revision 没有 `auto_review`，行为与今天一致，需要重新解析才获得。重新解析会生成新 revision，当前草稿的逐页核验进度会清零。

## 替代方案

- 只去掉"解析器排除的页眉强制说明"，不引入机器核验：人工仍需逐页勾选三项，页数不减少，未采纳。
- 保留按出现次数统计的缺字比对、只调整比例阈值：重复文字永远算缺失，任何阈值都无法同时容纳期刊页（重复刊名）与表格页（重复表头），未采纳。
- 空白页只标记不自动放行：105 页里 11 页空白仍需逐页点击确认，与诉求相反，未采纳。
- 空白页按"无文本层且无正文"放行，不要求可视内容证据：OCR 失败的扫描页会被静默放行，未采纳。
- 维护"会被归一化的码位"排除清单（曾按 Enclosed Alphanumerics + 全角字母数字实现）：清单无法覆盖同一份文档里既被保留又被归一的罗马数字，实测把 17、55、86 三页的归一化误判为缺失（把 41 页降到 38 页机器核验）而检测力并无提升，改用两侧 NFKC 同一形态比对，未采纳。
- 文档级一次性确认、放弃逐页核验：会丢掉块级来源映射与每页核验人/时间，检索答案的引用无法回溯到页与文块，未采纳。

## 后果

- 本决策部分取代 [PDF 结构化解析与逐页审核](./2026-09-18-structured-pdf-review.md) 与 [审核校验前端镜像](./2026-09-20-structure-validation-frontend.md) 中"审核记录只可能来自人工判断"和"每页都必须人工核验"的表述；两者的其余要求（来源不可变、审批与索引边界校验、前端镜像后端契约）继续有效，两份记录已就地标注并链接到本记录。
- 该 105 页文档（MinerU 解析、仅 3 页人工签核）按当前口径重算：机器核验 67 页（`no-anomaly/v1` 56 页、`blank-page/v1` 11 页），需人工 38 页，其中含表格或图示 31 页、独立文本层显示内容缺失 47 页（两者大量重合）。跨页续排造成的 17 页误报被放行，同时新发现 2 页（第 11、92 页）旧口径漏掉的内容缺失。人工操作从 105 页降为 38 页。
- 占位正文不再进索引：该文档 revision 1 有 90 个空结构块、分布在 65 页，其中 61 个此前仍处于"参与检索"状态。用维护性回填生成 revision 2（不改动已审核的 revision 1）后实测：正文里的占位句从 61 处降为 0，片段总数从 537 降至 495（去掉 42 个由占位句形成的片段），含占位句的片段从 62 个降到 1 个——剩余那 1 个是第 77 页的两个图示占位，该页人工签核时写的是"确认"、既未补充也未排除，按设计图示占位不自动排除，留待人工决定。同一次回填顺带清掉 11 个空白页上臆造的占位文块（这些页现在没有任何文块，只保留空白页结论与补录入口）。
- 去重字符集口径下该文档的覆盖分布（94 个可比对页）：等于 1.0 有 43 页；[0.995, 1.0) 有 3 页（最多缺 2 个字符）；[0.99, 0.995) 有 3 页（最多缺 3 个）；[0.98, 0.99) 有 8 页（最多缺 7 个）；[0.97, 0.98) 有 5 页（最多缺 11 个）；[0.95, 0.97) 有 7 页（最多缺 17 个）；[0.90, 0.95) 有 14 页（最多缺 29 个）；低于 0.90 有 11 页（最多缺 80 个），另有 11 页无文本层。阈值 0.99 落在分布较稀疏处；机器核验的 41 页里只有 2 页仍缺字符（各 1–2 个），缺字数字与样例始终显示在页面上，可复核可收紧。
- 审计如实区分来源：`reviewed_by`/`reviewed_at` 只由人工勾选产生，机器结论单独记在 `auto_review` 且带解析器身份与时间，两者不互相冒充。
- 机器核验只覆盖"该页没有需要判断的东西"与"解析结果未覆盖字符低于 1%"两类事实，不宣称语义正确：比对按字符集合进行，无法发现"文字齐全但顺序错乱或块被重复"的问题，这类错误仍依赖人工处理含跨页多来源、孤立文字等异常的页；表格行列、图示条件分支仍由人负责。
- 旧能力不存在：不再要求机器核验页通过三项勾选或填写页面说明；不再按字符出现次数报缺字；不再因解析器自动排除的页眉页脚要求人工说明。
- 重新引入条件：只有出现机器核验页漏放语义错误的真实案例，并提供对应负向证据时，才扩大人工核验范围或收紧规则；收紧方式为调整 `AUTO_REVIEW_MIN_COVERAGE`、`NON_BLOCKING_ISSUES` 或 `mark_auto_review` 的判定条件，不新增并行规则，也不恢复码位排除清单。
- 无独立文本层的扫描页仍要求人工：没有可比对的文本层就没有独立证据，不机器核验；含图像或图形的空白页同样要求人工确认。
- 表格与图示页一律人工：若实际使用中这类页的核对成本仍然过高，需要单独提案决定是否信任已成功提取的表格 HTML，不能直接放宽闸门。
- 超过 500 个不同字符的页，1% 比例比此前的绝对 5 个字符更宽松；该文档最大 442 个不同字符，尚未触及。

## 验证

- 规则与闸门：`test/unit/knowledge/test_structured_pdf_review.py` 覆盖规则命中与不命中、阈值边界、闸门拦截、人工接管失效。关键用例：`test_machine_review_clears_only_pages_without_anomalies`（期刊页命中、空白页命中；表格页、缺内容页、带结构异常的空白签名页、缺 `has_visual_content` 证据的页、低覆盖但无 issue 的页都不命中；两页未签核时闸门以"第 3 页尚未完成"拒绝，补签后通过）；`test_machine_review_invalidated_by_human_edit`（未改动页保留核验且 `reviewed_by` 为空；改正文、改类型与层级、改排除、新增文块、改页内顺序、写页面说明六种人工介入都使机器核验失效并被闸门拒绝）；`test_native_coverage_tolerance_is_ratio_based`（比例阈值边界：缺 2 字放行、缺 4 字超出阈值）；`test_native_coverage_checks_whole_document_and_normalizes_forms`（跨页续排段落不算缺失、`Ⅳ`/`⑴` 与 `IV`/`(1)` 视为同一内容、整句真丢失时给出原文片段、表格 HTML 标签不参与比对）；`test_native_coverage_records_absent_characters_and_scanned_page`。
- 解析器接入点：`test/unit/services/test_ocr_service.py` 的 `test_mineru_clean_page_is_machine_verified`（MinerU 分支：页眉为排除文块、`absent_characters == 0`、正文不含期刊名、闸门通过），并在既有 MinerU 用例中断言未返回原页 PDF 时没有 `auto_review`（无独立证据不放行）。
- 前端：`web/test/unit/structured_review.test.js` 的 `机器核验页免人工核验：校验与跳页跳过，页面说明或编辑可接管`，断言审批校验只列出待人工页、页标签与汇总区分两种来源、覆盖证据按两位小数展示、跳页跳过机器核验页、写页面说明后该页重新要求三项核验、在机器核验页上主动勾选三项不被要求填写说明。
- 真实链路（真实 PostgreSQL、MinIO、Milvus、真实 ARQ worker、本地 Docling）：合成两页 PDF（每页含页眉 `JOURNAL OF TESTING 2021 VOL 1 NO N`、标题与正文）经 `add_file_record` → `knowledge_parse` 任务解析 → 两页均为 `no-anomaly/v1`；对第 2 页做人工修订后该页失效、第 1 页保留；`approved_revision` 以 `ReviewConflict("请先审核最新清洗稿，再执行入库")` 拦截；补签后审核通过，`knowledge_index` 任务把 2 个片段写入并回读到含"人工修订"的正文，文件终态 `indexed`。探针为临时脚本、未提交；可复现形式是下面的 HTTP integration 用例。验证对象为临时知识库，事后删除任务行、KB/文件/chunk 行、MinIO 对象与 Milvus collection，确认无残留。
- 真实文档口径复核：以真实 105 页文档的解析结构重新执行 `check_native_coverage` + `mark_auto_review`（含用 PyMuPDF 复算可视内容证据）。口径迭代三次：码位排除清单 38 + 11、NFKC 单页字符集 41 + 11、按连续片段在全文档查找 56 + 11。最后的口径清掉了跨页续排误报（相对上一版 17 页由人工转为机器核验），并新标记出 2 页旧口径漏掉的内容缺失。独立 Reviewer 在两轮复算中分别确认了当时口径的数字（45 + 11 与 41 + 11），并独立抽查确认 11 个空白页在原 PDF 中无文本层、无图像、无图形。
- 占位文块：`test/unit/knowledge/test_mineru_structure.py::test_empty_block_is_excluded_so_placeholder_never_reaches_content`（空块被排除、说明为解析器文案、审核稿正文里没有占位句、只有一个非排除文块）；`test/unit/knowledge/test_structured_pdf_review.py::test_placeholder_block_cannot_be_indexed_without_text`（取消排除被拒，补充原文后放行且正文只含补录内容）；同一文件的规则用例额外断言空白页的文块与提示被清空、有可视内容的页保留文块；`test/unit/services/test_ocr_service.py::test_page_has_visual_content_separates_blank_page_from_image_page`（空白页为假、含图页为真）；前端 `占位文块不能参与检索：占位正文未补充时保存被拦截`（占位未补充时保存被拦、勾选排除并写明原因后放行、补充原文后可取消排除）。占位文案与说明集中到 `structure.py` 常量，两个解析器共用，不再各自维护字面量。
- 维护性回填（真实 PostgreSQL，非 HTTP）：读取 revision 1 → 61 个占位文块标记为排除 → 用源 PDF 重算逐页文本层证据（`check_native_coverage` + `page_has_visual_content`）→ 清空旧 `auto_review` 后重算规则（含空白页清理）→ `structure_report` 重算正文与来源映射 → 写入 revision 2 草稿。共运行三轮（口径与清理逻辑逐步修正），每轮先删除上一轮的草稿版本。最终 revision 2 相对 revision 1：正文占位句 61 → 0，片段 537 → 495，可用文块 1706 → 1645，53 页人工签核与说明原样保留，机器核验 52 → 67 页，第 11、92 页因新口径发现的内容缺失转为待人工。revision 1 未被改动，回滚只需删除 revision 2。最终一轮的 `require_structure_review` 按预期拦住 revision 2（第 11 页未签核），确认闸门仍然生效——草稿可以带未核验页存在，审批必须补齐。
- 命令与结果：
  - `docker compose exec -T api python -m pytest test/unit -m "not slow" --ignore=test/unit/services/test_run_worker.py -q -p no:cacheprovider`：2146 passed，53 skipped（`uv run` 在本机容器内因可编辑安装的 `.pth` 文件权限失败，改用容器内 `python -m pytest`）。
  - `... pytest test/unit/knowledge test/unit/services/test_ocr_service.py test/integration/services/test_document_review.py -q`：308 passed。
  - `... pytest test/integration/api/test_structured_document_review.py -q`：2 skipped（缺 integration 凭据）。
  - `docker compose exec -T web pnpm run test:unit`：360 passed；`lint:check` 退出码 0；`build` 退出码 0。逐页审核组件与面板单测 8 passed。
  - `uvx ruff@0.16.4`：三条 CI 门禁 `check package`、`format package --check`、`check --select I package` 全部通过。CI 的 ruff 作业只检查 `package`；仓库既有漂移（`milvus.py` 的 E501、`chat_service.py` 与 `document_review_service.py` 的换行、11 处导入顺序）已在同一变更内一并清理，`test/` 与 `server/` 下另有 23 个文件会被 `ruff format --check` 重排、24 条 lint 报错，它们不在任何门禁范围内，未处理。
  - 前后端契约漂移守卫：`scripts/verify_engineering_contracts.py` 的 `_validate_review_contract_constants` 比较 `structure.py` 的 `AUTO_REVIEW_RULE_*`、`PLACEHOLDER_PREFIXES` 与 `StructuredDocumentReview.vue` 中镜像的 `AUTO_REVIEW_LABELS`、`PLACEHOLDER_PREFIXES`。守卫按常量名取全部规则 id（新增规则不改守卫也能被发现）、接受单双引号与 `Object.freeze([...])` 包装、只扫 `AUTO_REVIEW_LABELS` 对象字面量（避免其它 `'xxx/v1':` 键误报），并且**双侧都缺才跳过**：只剩一侧、或两侧在但解析为空都报"无法比对"——防漂移守卫解析不出来就等于没守。负向案例七项：规则 id 漂移、占位前缀漂移、后端新增规则而前端没镜像、前端改用双引号键、前缀被 `Object.freeze` 包装、只剩前端一侧、常量被改名导致解析为空（后五项都是早期实现会静默放过的形态）。
  - `python3 scripts/verify_engineering_contracts.py`：通过（123 decisions / 5 workflows / 4 agents files / 175 docs / 25 routers / 252 web sources / 5 review contract constants）。此前两处既有失败（`2026-09-20-review-draft-versioning.md` 类型为 `simplification` 却缺「旧能力不存在：」「重新引入条件：」）已在该记录内补齐。
  - `python3 -m unittest scripts.test_verify_engineering_contracts`：Ran 69 tests，68 passed、1 error（含上述七项负向用例）；Windows 宿主因无创建符号链接权限，`test_decision_owner_symlink_cannot_escape_repository` 报 WinError 1314，属环境限制——干净 HEAD 上同一用例同样失败，与本次改动无关。
  - 文档链接：根目录与 `docs/`、`web/`、`backend/` 下的 184 个 Markdown 文件（含未跟踪文件）共 265 条相对链接、0 死链（独立复核者按另一口径扫到 182 个文件 256 条链接、同样 0 死链）；此前 `proposed/2026-09-18-clinevidence-thread-patient-binding.md` 的 `./../../../../AGENTS` 死链已修正为 `../../../../AGENTS.md`。

## 未解决

- 真实 HTTP integration `test/integration/api/test_structured_document_review.py::test_machine_verified_pages_need_no_human_signature`（解析 → 免签审批 → 改动后拦截 → 补签 → 入库的 HTTP 全链路）已写入但本机未执行：integration fixture 需要 `TEST_USERNAME`/`TEST_PASSWORD`，当前环境未配置，测试按既有约定 skip。同一契约由上面的真实链路（worker + repository + Milvus，缺 HTTP 层）与 unit 覆盖，接入凭据后应重跑并以其结果为准。
- 后端全量 unit 未包含 `test/unit/services/test_run_worker.py`：该文件在全量运行时挂起，单独运行通过（50 passed，13.34 秒），与既有多份记录描述的本机卡点一致，本次未处理；本次改动不触及该文件的模块级导入链（收集成功即为证据）。
- 空结构块不再产生页面级 issue 后，解析器侧"这一区域没取到文字"只体现在文块说明与排除标记上；若解析器把一个原本有内容的区域判为空，人工只能靠逐页阅读发现。当前依赖独立文本层比对（缺字证据）覆盖该风险，未单独构造"解析器误判整块为空"的负向样本。
- 上面的真实链路验证是直接调用服务与仓储层完成的，未经 HTTP 路由与权限依赖；路由本次未改动，但"管理员经 API 完成上述流程"仍属未验证范围。
- 比对按字符集合进行，因此"文字齐全但页内顺序错乱或文块被重复"不会被文本层比对发现；这类页若同时没有结构异常就会机器核验。当前依赖解析器自身的结构 issue、表格与图示判定来覆盖，未单独构造乱序或重复的负向样本。
- 文本层比对判定的是「这段连续文字在解析结果里找不到」，不区分「整段丢失」与「内容在、但被解析器切分或重排成别的顺序」；实测该文档 275 个被标记片段里 195 个在全文中连 6 字窗口都找不到（确定丢失），80 个存在局部匹配（需人工确认是重排还是部分丢失）。抽查到的样例在解析结果中确实整段不存在。
- 既有已解析文档（含本记录引用的 105 页版本）不会自动获得机器核验，需要重新解析；重新解析会生成新 revision 并清空当前草稿的核验进度。
