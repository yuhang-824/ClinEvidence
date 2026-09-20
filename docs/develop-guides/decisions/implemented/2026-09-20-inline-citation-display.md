# 回答引用：行内证据标记与页码

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/skills/buildin/knowledge-base/SKILL.md

## 问题

知识库问答给出了正确但无法核验的回答：模型写"根据指南……"，而医生看不到依据来自哪份文件、哪一页，只能自己回原文翻找。原有实现（`buildin/chatbot/prompt.py` 的 `SOURCE_CITE_PROMPT`）定义过 `<cite source="..." type="file">1</cite>` 约定，前端也早已写好 `cite` 样式与 DOMPurify 放行，但该常量从未被引用，注释写"效果不好，暂时不启用"；标记里没有页码，点击没有行为，`source_metadata`（含 `pages`）在前端归一化时被丢弃，来源面板因此只能显示文件名。

## 决策

复用并补完既有约定，不新造一套标记语法。

**标记格式**：`<cite source="文件名.pdf" data-page="12" type="file">1</cite>`。`data-page` 是新加字段，用 `data-*` 是因为它默认通过 DOMPurify、无需改清洗配置，也符合点击委托的取值习惯。

**指令归属**：写在 `knowledge-base` Skill 的 SKILL.md（模型使用知识库工具前会读它，作用域正确），明确要求：每条基于检索内容的论断句末附引用；`source` 取片段 `metadata.source` 原值；`data-page` 取该片段 `metadata.source_metadata.pages`；序号从 1 递增、同片段复用；没有页码的片段（历史片段、外部只读库）省略 `data-page`；只能引用检索结果里出现过的文件名与页码。同时删除 `SOURCE_CITE_PROMPT` 死代码——同一契约不留两个 Owner，其"效果不好"的注释会误导后来者。

**前端展示**：`MarkdownPreview` 的 `cite` 样式改为品牌色胶囊（`--main-50` 底、`--main-700` 字、1px 边框、12px/500），页码用 `::after` 按 `attr(data-page)` 显示——形状与文字双重区分，不只靠颜色，且渲染缓存仍只依赖模型原文。新增 `enhanceCitations()`（沿用 `enhanceKbImages` 的幂等写法）补 `role`/`tabindex`/`title`，`handleMarkdownAction` 的委托加 `cite[source]` 分支并支持 Enter/Space。

**证据卡片**：`MarkdownPreview` 只 emit `cite-click`，卡片由 `AgentMessageComponent` 渲染（它已持有本消息的工具结果与来源组件）：文件名 · PDF 第 N 页、章节、片段原文，底部"打开原文（跳到第 N 页）"。跳转复用 workspace 的 `/api/workspace/knowledge/file`（普通用户凭 uid 可见性校验即可用）在 `PdfPreview` 里滚动到该页，不给普通用户新开按页取图接口，权限面不变。

**引用解析**：`utils/citation.js` 提供纯函数 `resolveCitation(chunks, {source, page})`，按文件名匹配、优先页码命中的片段；文件名不存在返回 null、页码不在任何片段里则标记 `pageMatched: false`，卡片分别提示"未找到对应片段"与"该页码没有出现在检索到的片段里"——伪造引用不会被当成证据展示。`messageProcessor` 的片段归一化改为保留 `source_metadata` 与 `kb_id`，并把内置 `query_kb` 结果纳入来源提取（此前它只按"工具名等于库名"匹配，内置知识库的片段根本进不了来源面板）。

**流式防护**：`stripIncompleteCitation(text)` 在渲染前截掉末尾半截的 `<cite`/`</cite` 片段。平滑输出按字素切片，半截标签会短暂显示成字面 HTML，这很可能就是该功能当初被停用的原因之一。

**召回质量可见性**：引用卡片要求「能看到依据有多相关」，而分数此前没有接通。内置检索工具按 `SearchResultSchema` 返回，顶层只有 id/kb_id/file_id/content/metadata，分数落在 `metadata.score`；前端归一化只读顶层 `score`，于是分数全空——来源列表的「按分数降序」退化成保持工具返回顺序，片段卡片也不显示相似度。归一化改为 `chunk.score ?? metadata.score`、`chunk.rerank_score ?? metadata.rerank_score` 并在 metadata 里保留，来源列表恢复真实的相关度降序；分组排序从文件名改为「该文件内最高分」（同分退回文件名，缺失分数排最后），文件内仍保持检索顺序。另外 `build_search_output` 此前只透传 `score`/`distance`，把顶层 `rerank_score` 丢掉了，现在一并透传——调用方可以在同一条列表里比较向量顺序与重排顺序，据此判断是否需要重排模型；检索测试页本来就同时显示相似度、重排分与实际生效的检索模式。

## 替代方案

- 新造 `[《文件名》第12页]` 之类的纯文本标记再自行解析：与既有 `<cite>` 约定、已写好的样式和清洗白名单重复，且要自己处理与正文的边界，不采用。
- 后端把引用解析成结构化证据再下发给前端：需要新增引用数据模型、解析与校验链路，且模型仍需先产出标记；先把标记与展示跑通，等确认模型产出稳定再考虑，不采用。
- 点击引用直接打开原文（跳过卡片）：医生缺少"哪句话来自哪个片段"的中间证据，只剩页码可看，不采用。
- 不做流式防护、只在终态渲染引用：运行中会持续显示半截标签，可读性受损，不采用。

## 后果

- 医生读到的每条论断后面跟一个品牌色胶囊（`1 第12页`），点开能看到该论断对应的检索片段原文与来源页码，并可跳到原文该页核对。
- 机器产出不稳定时**不会退化成错误信息**：模型不写引用就只是没有标记；写了引用而文件名/页码对不上时，卡片明确提示引用可能不准确，而不是把检索到的任意片段当成证据。
- 引用只做**可解析性**校验，不做"这段话是否真的支撑该论断"的语义校验；伪造一个真实存在的文件名与页码仍会显示该片段。
- 旧回答（生成于本次改动之前）没有引用标记，回读时自然无胶囊；来源面板因保留 `source_metadata` 反而比以前多显示 PDF 页码。
- SKILL.md 改动需要重启 API 与 worker 才生效（启动时投影内置 Skill）；已在本地重启并确认投影内容包含引用规则。
- 旧能力不存在：`SOURCE_CITE_PROMPT` 常量已删除；来源面板不再丢掉页码；`query_kb` 的片段不再因为工具名不等于库名而被排除在来源之外。
- 重新引入条件：若模型产出的引用长期不可用（格式不合、页码乱填），应当在后端把引用解析成结构化证据并加校验，而不是放宽前端；若需要"引用支撑度"校验，需要单独的方案与证据。

## 验证

- 纯函数与渲染：`web/test/unit/citation.test.js` 7 项——按文件名与页码匹配片段；文件名不存在返回 null、页码未命中标记 `pageMatched: false`、空候选与缺 source 不命中；引用属性解析（缺 source 返回 null、非法页码只保留文件名）；流式半截标签（`<cite source=`、`<cit`、`</cit`、`</cite`）被截掉而完整标签与普通 `<` 文本保留；`createMarkdownRenderer` 渲染后 `<cite source data-page type>` 原样保留；`query_kb` 结果经来源归一化后保留 `pages`/`kb_id` 且能直接支撑引用解析。
- 后端：`test/unit/toolkits/test_kbs_tools.py` 断言检索结果的 `metadata.source_metadata.pages` 进入模型可见的工具结果（防止将来加白名单把它过滤掉）。
- 相关度排序：`web/test/unit/citation.test.js` 断言来源归一化把 `metadata.score`/`rerank_score` 提到顶层并按相关度降序；`web/test/unit/kb_result_groups.test.js` 断言文件按最高分排序、缺失分数排最后、同分退回文件名、同文件内保持检索顺序。
- 命令：`docker compose exec -T web pnpm run lint:check` 通过；`pnpm run test:unit` 353 passed；`pnpm run build` 通过。后端 `test/unit/toolkits/test_kbs_tools.py` 27 passed；`test/unit -m "not slow" --ignore=test/unit/services/test_run_worker.py` 见下方未解决项的说明。
- SKILL.md 投影：`docker compose restart api worker` 后，容器内 `skill-sources/shared/knowledge-base/SKILL.md` 含"回答必须标注引用"章节与 `data-page`（各 1 处命中），`/api/system/ready` 返回就绪。

## 未解决

- **真实页面验证未执行**：本地实例的登录凭证未知（integration/e2e fixture 都要求 `TEST_USERNAME`/`TEST_PASSWORD`，且没有自举路径），因此没有在浏览器里跑完"提问 → 行内胶囊 → 证据卡片 → 跳到原文第 N 页"以及浅/深色与窄屏截图。这是 `web/AGENTS.md` 对 UI 改动的要求，需要在拿到凭证后补做。
- **模型产出合规性未验证**：探测模型是否真的输出 `<cite ... data-page="N">` 需要模型供应商凭证（容器内没有 `DEEPSEEK_API_KEY` 环境变量，凭证现由模型供应商配置保存）。目前依据是规则明确、含完整示例，且首轮已被证明可停用——因此这条必须用一次真实提问确认；若产出不稳定，调用方与展示层都不需要改。
- DOMPurify 对 `cite`（默认白名单）与 `source`（既有 `ADD_ATTR`）的放行沿用既有配置，`data-page` 依赖默认的 `ALLOW_DATA_ATTR`；仓库测试环境没有 DOM 实现，无法在单测里跑真实清洗路径，只能靠浏览器确认——若被清洗，胶囊会丢掉页码（可见失败，不会静默）。
- 引用卡片当前用 `a-modal` 呈现。计划阶段设想的 hover 浮层需要把锚点 DOM 坐标传出来再自行定位，实现成本更高且移动端不友好，先用模态；若实际使用中觉得打断阅读，再改为就地浮层。
- 语义支撑度校验、引用去重编号的强制校验（同一片段必须复用同一序号）都只靠指令约束，未在后端校验。
- 相似度是检索侧的排序分（向量距离、混合加权分或重排分归一化后的值），不是「该片段能回答这个问题」的概率；判断召回准确率仍需要人工看片段内容，或走评估模块建立数据集与指标。
