# 文件列表自动刷新感知任务状态

状态：implemented
类型：bug-fix
Owner：web/src/stores/database.js

## 问题

上传文档提交解析后，文件管理列表不显示处理中的文件，必须手动刷新页面才出现。根因是时序竞态：`POST /documents` 只入队任务不创建文件记录，文件记录由 worker 执行任务时创建；而 `addFiles` 启动自动刷新后，1 秒的延迟刷新取到空列表且 `processing_count` 为 0，`ensureAutoRefreshForProcessing` 把"无处理中文件"判定为无事发生，关闭了刚启动的自动轮询。几秒后 worker 创建文件记录时已无轮询在跑。

## 决策

文件列表自动轮询的存续叠加任务状态维度：`hasActiveKnowledgeTask`（pending/queued/running 的 `knowledge_ingest`/`knowledge_parse`/`knowledge_index` 任务，`payload.kb_id` 匹配当前库）为真时，`ensureAutoRefreshForProcessing` 不因空列表关闭轮询，`runAutoRefreshTick` 不按无进展退避；活跃任务消失且轮询仍开启时立即补一次后台刷新拿到最终文件状态，随后的常规判定再关闭轮询。轮询的启停、退避与手动开关（`autoRefreshManualOverride`）语义不变。

## 替代方案

由 tasker store 在任务结束时直接调用 database store 刷新：方向反转为任务模块依赖业务模块，且 tasker 不知道当前页面在哪个库，不采用。前端上传后轮询单个任务详情直到终态再刷新列表：与 tasker 轮询重复建连，且批量任务部分失败时终态判定复杂，不采用。后端在文件状态写入处失效统计缓存：统计头部的更新滞后属于后端缓存策略，单独记录不在本次前端修复内。

## 后果

文件记录由 worker 创建后最多一个轮询周期（10 秒）内出现在列表，不再依赖手动刷新。任务执行期间轮询保持 10 秒节奏不退避，任务全部结束后自动停止，无僵尸轮询。手动关闭自动刷新的用户仍不会被自动打开（manual override 优先）。已知限制：头部"N 文件/N Tokens"统计来自 Redis TTL 缓存（10 秒）且无主动失效，任务刚结束时补刷可能取到过期值，需等缓存过期后的一次刷新才校正。

## 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| 任务执行中空列表不关闭轮询 | 竞态关闭轮询 | database.js | unit 断言 autoRefresh | 旧实现同场景 autoRefresh=false | Passed |
| 文件记录出现后列表自动可见 | 需手动刷新 | database.js | 真实页面上传观察 | 上传后不刷新页面等待出现 | Passed |
| 任务结束补刷并自动停止 | 僵尸轮询 | database.js | unit 断言补刷与 autoRefresh=false | 任务终态后轮询不停 | Passed |

## 验证

- `docker compose exec -T web pnpm run test:unit`：337 passed、0 failed，含新增 `知识库任务执行期间文件列表自动刷新保持存活，任务结束后补刷新并自动停止`（覆盖入队保活、文件出现、终态补刷、自动停止四个断言）。
- 负向案例在旧实现上失败：`git checkout` 回退 `database.js` 后执行 `node --test test/unit/database_store.test.js`，新增用例失败（旧行为把 autoRefresh 关闭）；恢复后通过。
- `docker compose exec -T web pnpm run lint:check`：通过。`pnpm run build`：通过（既有大 chunk 提示，非本次引入）。
- 真实页面验证（Docker 全链路）：以验证账号登录，新建知识库"实时刷新验证"，经上传对话框注入合成 PDF（`verify_refresh.pdf`，1058 字节）提交，关闭对话框后不做任何操作，文件行在约 4–8 秒内自动出现在文件列表并显示"待入库"（解析已完成），全程无手动刷新；截图存档。验证后已删除该文件与知识库。

## 未解决

- 头部统计（file_count/token_count 等）来自 `get_kb_file_stats` 的 Redis TTL 缓存（`knowledge_file_repository.py`，10 秒）且无写入侧失效，任务刚结束的一次补刷可能读到过期统计。修复属于后端缓存策略（在文件状态写入处失效或缩短 TTL），与本前端改动分开处理。
- 普通点击在新库卡片与对话框按钮上存在超时现象（本次验证通过页面脚本点击绕过），疑似与遮罩层相关的前端交互问题，未在本记录范围内定位。
