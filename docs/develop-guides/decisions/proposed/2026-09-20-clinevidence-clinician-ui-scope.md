# ClinEvidence 医生工作台收紧：zcode 执行任务书

状态：proposed
类型：simplification
Owner：web/src/layouts/AppLayout.vue

## 问题

读者是负责实施的 zcode 和代码 Reviewer。本文件是尚未执行的产品收紧提案，依据 2026-09-20 工作区源码及用户提供的对话首页截图编写。截图只用于辨认页面，不作为代码指令。任务目标是让医生通过资料、问题、证据与结果使用系统，减少理解项目目录、智能体配置、Skills 和沙盒的负担。

当前界面以通用 Agent 平台组织：项目选择、个人空间、Agent 管理、模型选择、审批模式和扩展配置占据主要入口。后端尚未形成患者、就诊、快照与质控业务闭环；现有文档解析审核属于知识入库质量审核，不是病历质控。

本轮验收目标：医生主界面不再暴露开发工作区及自由配置入口；新对话、附件、知识问答、历史、引用和异常状态仍可用；管理员能维护知识库和运行配置；为后续患者业务保留明确接入位置。非目标：本轮不实现患者数据库、诊断算法、质控规则引擎、EMR 接口，不删除历史数据，不改造整个设计系统。

前置阅读：[架构](https://github.com/yuhang-824/ClinEvidence/blob/main/ARCHITECTURE.md)、[阶段计划](../../clinevidence-roadmap.md)、[患者绑定提案](2026-09-18-clinevidence-thread-patient-binding.md)、[Web 约定](https://github.com/yuhang-824/ClinEvidence/blob/main/web/AGENTS.md)、[设计规范](../../design.md)、[测试规范](../../testing-guidelines.md)。源码优先于旧计划；执行前重新读取当前版本。

## 提案

### 执行边界与实施前检查

1. 先执行 `git status --short`、`git branch -avv` 和 `git log -5 --oneline`。本次审查时存在未提交的 MinerU、OCR 服务、上传组件和测试改动；不得覆盖、回滚或混入自己的提交。需要修改同一文件时保留原有变更，并单独审查本任务差异。
2. 新建 `codex/clinician-ui-scope` 分支，已有同任务分支则继续使用。脏工作区不执行自动 stash、reset 或 clean；需要隔离时按仓库工作树约定处理，并明确未提交改动不会自动复制。
3. 本轮先实施下面的 A、B、C 包，D 包是后续医疗功能的接入约束，不自动实施。每包完成后验证再推进；不要用大规模重命名替代业务收紧。
4. 默认使用现有普通用户、管理员、超级管理员权限，不新增“医生/主任”角色体系。医生工作台是产品使用视图；管理员也默认进入相同工作台，通过独立“管理”入口维护配置。
5. 表格中的“移出医生界面”指移除导航、组件、弹窗、快捷入口及医生页面不再需要的请求。它不等于撤销 API 权限。新增或收紧服务端授权必须检查现有 consumer、单独列明兼容影响并补真实 HTTP 证据，禁止宣称隐藏按钮已完成授权隔离。

### 冗余清单与处理决定

以下路径均相对仓库根目录，供符号搜索定位；执行者沿真实调用链确认其他引用，不把列表当作完整文件清单。

| 当前设计 | 对医生的负担 | 本轮处理 | 主要源码入口 |
|---|---|---|---|
| 输入框“选择项目”、创建项目、关联已有目录、从历史添加 | 要求先理解文件工作区，无法表达患者身份 | 从医生新对话移除，使用已有隐式 Project 创建路径 | `web/src/components/ProjectSelectionSection.vue`、`web/src/components/AgentChatComponent.vue`、`web/src/views/AgentView.vue` |
| 侧栏项目分组、项目菜单与项目化新建对话 | 将病历讨论组织成开发项目 | 医生端按对话历史展示；合并展示原项目中的历史会话，避免隐藏后丢失入口 | `web/src/components/ConversationNavSection.vue`、`web/src/utils/projectConversationGroups.js`、`web/src/layouts/AppLayout.vue` |
| “个人空间”与目录新建、文件编辑、目录绑定 | 以文件系统代替业务资料 | 移出医生主导航；保留现有存储与历史附件访问，运维入口与医生流程分开 | `web/src/views/WorkspaceView.vue`、`web/src/components/workspace/WorkspaceSidebar.vue` |
| 对话侧栏“个人空间/对话目录”文件树 | 暴露目录层级和技术路径 | 医生侧栏收敛为“本次资料”和只读预览；只列当前对话附件及其输出 | `web/src/components/AgentPanel.vue` |
| 上传弹窗“个人空间”来源 | 可以把任意工作区文件当作临床材料 | 医生入口保留本机上传；管理端知识文件导入能力按现有用途保留 | `web/src/components/FileUploadModal.vue`，先区分调用方，不能全局删选项 |
| “保存到个人空间”产物按钮 | 结果交付依赖目录管理 | 医生侧优先“预览/下载”；没有报告业务时不得改名为“归档病历” | `web/src/components/AgentArtifactsCard.vue` |
| 侧栏“智能体”及自由创建、编辑 Agent | 医生被要求配置工具而不是提出临床问题 | 移到管理入口；医生新对话使用服务端认可的默认 Agent | `web/src/views/AgentManageView.vue`、`web/src/components/model-management/AgentManagePanel.vue` |
| 输入框“智能助手”切换及管理捷径 | 一个工作入口出现多个不清楚的执行主体 | 移除医生下拉选择；显示固定助手名称即可，原会话绑定继续保留 | `web/src/views/AgentView.vue` |
| 输入框模型选择、供应商技术名称 | 把部署选择推给医生 | 模型由管理员配置；未配置时给出明确不可用提示，不随机选列表第一项 | `web/src/components/AgentChatComponent.vue`、`web/src/components/ModelSelectorComponent.vue` |
| “请求审批/完全信任”模式 | 技术审批容易被理解为临床审核 | 移出医生输入框；保留已有审批执行语义和必要的人机确认 | `web/src/components/ToolApprovalModeSelector.vue`、`web/src/components/HumanApprovalModal.vue` |
| “知识库·技能”混合导航 | 文献知识与运行扩展混为一谈 | 管理端分清“知识库管理”与运行配置；医生仅消费有权限的知识证据 | `web/src/views/ExtensionsView.vue`、`web/src/layouts/AppLayout.vue` |
| Skills 安装、编辑、工具与 MCP 配置 | 与诊疗操作无关，可改变模型能力 | 移出医生流程；管理员维护现有配置，不破坏内置 knowledge-base Skill | `web/src/components/extensions/`、`web/src/views/ExtensionsView.vue` |
| 输入框 @ 同时选择文件、知识库、技能/MCP | 需要理解内部能力激活机制 | 医生保留本次附件与有权限知识来源；移除技能/MCP 的候选和激活入口 | `web/src/composables/useAgentMentionConfig.js`、`web/src/components/MessageInputComponent.vue` |
| 定时任务 beta、cron 调度 | 当前没有对应的临床任务产品 | 医生不提供新建；账户下为原所有者保留既有任务的查看、暂停、删除，不擅自停止运行 | `web/src/views/ScheduledAgentsView.vue`、`web/src/views/AgentManageView.vue` |
| 沙盒环境变量、API Keys | 暴露工程集成概念 | 移出日常设置；已有 Key 的所有者保留元信息查看与撤销入口，个人环境配置仍由原所有者维护 | `web/src/components/SettingsModal.vue`、`web/src/components/AgentEnvSettingsCard.vue` |
| AGENTS.md、USER.md、MEMORY.md 编辑指引 | 让使用者直接配置长期提示词 | 医生工作台不提供编辑入口；跨患者记忆隔离留给医疗后端专项 | `web/src/views/WorkspaceView.vue`、`backend/package/yuxi/agents/middlewares/memory.py` |
| Debug、完整模型输入、上下文 token 仪表、原始执行详情 | 技术信息干扰证据阅读 | 保留管理员诊断能力；医生只见进度、失败原因、来源与结果 | `web/src/components/AgentChatComponent.vue`、`web/src/components/AgentPanel.vue`；Debug 当前已受超级管理员及 debugMode 限制 |
| “引导”运行中追加消息 | 容易模糊本轮问题与执行范围 | 医生隐藏高级 steer 操作；保留发送、排队、停止和可理解的状态 | `web/src/components/AgentChatComponent.vue` |
| “数据总览” | 通用平台统计不等于医疗质量统计 | 保持超级管理员权限，移到管理入口，不改名为“科室质控看板” | `web/src/views/DashboardView.vue`、`web/src/layouts/AppLayout.vue` |
| Star 提示、GitHub 统计、通用欢迎语 | 干扰医生任务定位 | 工作台移除推广卡片；关于页保留开源致谢，欢迎语对应当前已实现能力 | `web/src/components/SettingsModal.vue`、`web/src/layouts/AppLayout.vue`、`web/src/components/AgentInputArea.vue` |

### A 包：导航与输入区收紧

医生主导航只保留“新建咨询”和“历史咨询”；账户、主题等基础设置继续可用。知识资料浏览仅在已有权限及可复用页面能完成只读查询时提供，不能将管理员知识库管理页直接开放给普通用户。管理员另见单一“管理”入口，复用现有页面维护知识库、助手配置、模型、OCR、用户和诊断；各功能沿用原权限，不能把超级管理员功能下放给普通管理员。

账户下按需保留“既有自动任务”和“既有集成配置”维护入口，供原所有者暂停/删除任务、撤销 Key 和维护个人环境配置；无既有资源时不展示，不提供医生新建入口。现有任务查询按 uid 限定，管理员身份不自动获得其他用户所有权，不能仅把菜单搬到管理端就宣称维护能力保留。这是存量资源维护例外，不进入医生主要工作流。

输入区保留正文、附件、发送/停止以及必要状态。移除项目、Agent、模型、审批模式选择器和高级运行控制；清理对应空插槽、无用布局高度和医生端专用监听。欢迎语建议“ClinEvidence，基于资料提供证据参考”；占位文案建议“输入问题，可添加本次咨询资料”。尚未有患者归属时不写“已绑定患者”“病历已归档”或“开始自动质控”。

沿 `AgentView.vue` 的 `route.query.project_id`、`initialProjectId`，以及侧栏创建入口检查隐藏绕行。医生新建咨询忽略旧项目预选入口并采用隐式 Project，历史会话继续读取原绑定。旧路由和查询参数应有明确重定向或说明，不能白屏、循环跳转或误把旧项目资料加载到新咨询。

默认助手与模型沿现有配置解析，不硬编码 ID，不让前端靠缓存猜选。新咨询必须能从干净浏览器会话启动；默认助手不存在、不可访问或模型未配置时明确提示管理员处理。旧会话保留原 Agent 和模型解析契约，不批量重绑。必要后端调整放在当前配置 Owner，禁止另建平行配置系统。

验收：普通用户与管理员默认工作台均无上述选择器；管理员经管理入口能配置默认值；已有配置下新咨询能完成真实请求，旧咨询仍可回读；清空 localStorage 不影响默认选择；无配置时不显示成功或无限 loading。

### B 包：资料与管理入口分离

用当前对话附件列表和已有预览能力组成“本次资料”，不要另建患者文件表。保留上传、解析中、成功、失败、重试及只读预览；附件失败不能显示“已纳入证据”。输出文件保留下载；证据来源保留原文、页码和版本中实际存在的字段，不合成坐标或来源。

医生端移除目录创建/编辑、个人提示词编辑、任意目录搜索和保存到个人空间入口。全局文件搜索、弹窗、右侧面板、移动端设置也要覆盖，不能只修改主导航。共用上传组件按实际调用方区分，保留知识管理员正在使用的解析、清洗、逐页审核、切片预览与入库流程。

保留服务端知识库权限、内置工具和 required Skill 装配；移除 @技能/MCP 后验证知识问答仍能真实调用知识工具，不能只凭普通模型回复判定成功。审计与 Run 状态继续落库；医生界面以资料处理、检索与回答等真实可观察状态呈现，缺少阶段事件时使用普通“处理中”，不虚构流程动画。

验收：历史附件和引用可打开，所有可见资料属于当前授权范围；医生无目录编辑入口；知识管理员完成一次合成文档审核入库，医生完成一次授权知识问答；失败、取消、断线恢复保持真实状态。

### C 包：回归、文档与交付

删除医生页面不再使用的 import、监听、请求和组件实例；共享组件只有在全仓搜索无当前 consumer 后才删除。更新真实行为拥有的架构和用户说明；本提案只有在全部本轮验收收敛后移入 implemented，并改写为当前决定和实际证据。未完成的医疗功能继续留在路线图。

将每包的实际命令、结果、截图、未验证范围和可复现缺陷写入交付说明。依据仓库规范，在代码 commit 前由不继承开发上下文的新 Reviewer Agent 独立审查完整需求、diff 和测试；修复影响功能、边界、证据或复杂度的问题。提交使用中文 Conventional Commit，不夹带已有解析器改动。未经用户授权不推送或创建 PR。

### D 包：后续医疗功能接入约束（本轮不实施）

后续医生信息架构以“患者与就诊 → 病历资料与快照 → 诊疗咨询 / 病历质控 → 证据与报告”为主线。患者和质控页面必须在相应后端契约实现后上线，本轮不添加可点击的空患者列表、假评分或演示成功接口。

| 后续界面 | 后端前置事实 | 上线验收 |
|---|---|---|
| 患者与就诊选择 | Patient、Encounter、授权可见性、导入归属 | 直调 API 不能跨患者读取；Project 不作为 patient_id |
| 当前患者上下文条 | 会话患者绑定、本轮快照与时间范围 | 展示服务端返回值；切换后旧摘要、附件及缓存不混入新患者 |
| 病历资料和时间线 | 页处理状态、文书类型、事件时间、来源片段、发布快照 | 未知日期显式保留；失败页不被当作缺项；旧引用可回读 |
| 医疗证据回答 | 患者/知识双通道工具、引用验证 | 证据对应患者及快照；信息不足可说明，伪造引用被拒绝 |
| 病历质控报告 | 版本化规则、全病历覆盖、确定性评分、持久任务 | 通过/不通过/不适用/待复核/无法判断/未实现分别显示，扣分可复算 |

本轮不替换既有患者绑定提案。临床角色权限、切换患者策略、医疗工具白名单和记忆隔离在相应后端任务中收敛；不能通过前端隐藏配置来宣称系统已限制工具执行或防止跨患者泄漏。

### 必须保留的内部机制

- Project 是现有 Conversation、Workdir、附件和 artifact 的持久化绑定。本轮只去掉医生的项目操作；复用 `backend/package/yuxi/services/conversation_service.py` 和 `project_service.py` 中隐式创建路径，不删表、不迁移路径、不将 Project 重命名为 Patient。
- Message、AgentRunRequest、FIFO、AgentRun、worker lease、取消、SSE、checkpoint 和同 Run 结果绑定继续保留；医生不需要看到其内部名称。
- 医生端移除审批模式切换不等于设为 `always_trust`。保留服务端解析和 pending interrupt 的确认流程，保留历史审批状态；新医疗只读工具及审批策略必须在服务端专项实现。
- 原始对象、审核版本、历史片段、知识来源及审计保留。医生的附件入口和管理员的知识入库入口是不同用途，不能全局禁用共用组件。
- 保留现有默认模型和模型审计；不因界面收紧更换供应商、删除凭据、启停定时任务或修改用户保存的 Agent 定义。

## 替代方案

| 方案 | 代价与结果 | 本提案选择 |
|---|---|---|
| 保留所有通用入口，只换医疗文案 | 用户负担不变，容易把目录误认成患者 | 不采用 |
| 只用 CSS 隐藏按钮 | 仍有快捷入口、无用请求和残留布局，无法验证整体体验 | 不采用 |
| 删除 Project、Workspace、Sandbox 等全部后端能力 | 牵涉既有附件、工具、历史数据和运行链路，超出本轮范围 | 不采用 |
| 收紧医生流程，管理端集中配置，保留内部绑定 | 可以分包验证，保留后续医疗业务接入空间 | 采用 |

## 验收标准

旧能力不存在：医生主流程、移动端、弹窗、@菜单、侧栏和新对话 URL 不再提供项目目录操作、自由 Agent/模型切换、完全信任、Skills/MCP 配置和沙盒环境配置。管理端及内部存储中保留的能力不算删除失败，必须在交付说明列明；导航隐藏不算服务端权限撤销。

重新引入条件：只有明确的医生业务用例和对应后端契约、权限与验证证据才能引入新入口；运维需求通过管理入口满足，不重新铺回医生输入框。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 医生入口已收紧 | 子菜单、移动端、URL 留绕行 | AppLayout、AgentView、路由 | 前端 unit + 普通/管理用户真实浏览器 DOM | 旧 project_id 链接重新打开项目选择即失败 | Not run |
| 隐式项目维持附件隔离 | 去掉选择后复用错误目录 | conversation_service、project_service | 真实 HTTP/worker，回读两会话绑定与附件 | B 会话能取得 A 私有附件即失败 | Not run |
| 历史会话仍可访问 | 删除项目分组后漏掉历史 | ConversationNavSection、历史查询 | 已有项目会话与普通会话逐一打开 | 项目会话数量或入口丢失即失败 | Not run |
| 默认助手可用 | 缓存残留掩盖无配置 | Agent store、现有配置解析服务 | 清空前端持久状态后真实请求及同 Run 结果 | 缺失默认值仍假成功或随机选助手即失败 | Not run |
| 审批语义未弱化 | 隐藏后变成完全信任 | 请求配置解析、HumanApprovalModal | 请求配置回读及 pending interrupt 回归 | 未确认工具直接执行即失败 | Not run |
| 知识链路和证据可用 | 去掉技能菜单导致工具不可用 | knowledge Skill、知识权限查询 | 合成资料入库、实际工具轨迹及来源回读 | 无权限知识库通过手工请求可读取即失败 | Not run |
| 管理权限不扩大 | 重组路由绕过原权限 | 后端依赖与 repository | 真实 HTTP 权限测试、各角色页面 | 普通用户获得配置写权限即失败 | Not run |
| 状态与资料真实 | 错误被成功样式掩盖 | 附件服务、Run、预览组件 | 取消、解析失败、重连的最终状态回读 | 失败附件显示已完成即失败 | Not run |

已有测试定位入口：`web/test/unit/chatStartScreen.test.js`、`projectSelection.test.js`、`projectConversationGroups.test.js`、`mention_utils.test.js`、`settings_lazy_mount.test.js`；均位于同一 unit 目录。后端从 `backend/test/integration/api/test_project_api.py`、`backend/test/e2e/` 及对应附件/审批测试按符号搜索选择。不删除反例来迁就 UI 变更；显式审阅旧界面断言的更新。

最低命令如下；按 [测试规范](../../testing-guidelines.md)补充改动相关真实 integration/E2E。本轮实现涉及会话创建、附件或运行行为时，unit 和 build 不能替代真实链路测试。

```bash
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
docker compose exec api uv run --group test pytest test/unit -m "not slow"
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
cd docs
pnpm run build
cd ..
git diff --check
```

真实页面至少覆盖桌面/窄屏、浅色/深色、普通用户/管理员、首次无历史、有项目历史、模型缺失、上传失败、停止运行、断线重连、历史引用预览。交付至少提供医生首页、资料侧栏、历史咨询、管理入口四种实际截图；截图不得包含真实病历或凭据。布局复用现有 CSS 变量和图标，不增加 UI 框架。

增加既有任务所有者验收：普通用户能找到原任务并暂停，回读持久状态确认停用；其他用户不能操作该任务。增加既有 API Key 所有者撤销验收；截图和报告不输出密钥正文。

环境失败记录准确原因，不静默修改依赖或扩大容器权限。若标准 uv 同步存在权限问题，可在确认依赖已安装后使用 `--no-sync` 执行相关测试，但必须同时报告标准命令失败及替代命令结果。Windows 符号链接测试可在支持该语义的 Linux 环境复核，不能把未运行写成通过。

## 风险

隐藏项目分组可能使已有会话失去入口；移除选择器可能暴露默认配置缺失；共享上传组件调整可能破坏知识库导入；移除 @技能可能影响知识工具激活。以上风险分别由历史回读、干净浏览器初始化、管理端入库和真实工具轨迹验收。

保留的通用后端能力仍可能通过已有 API 使用。本轮提供界面收紧，不宣称医疗工具白名单、患者隔离或临床有效性已经实现。若实施中发现必须改变持久状态或授权边界，先补对应提案及证据矩阵，再实现必要的最小变更。
