# ClinEvidence 初始化验证记录

记录日期：2026-09-17。范围是源码导入、项目品牌与阶段计划；医疗功能仍待实现。本记录只反映实际执行结果，不能替代后续真实服务验收。

## 源码与发布基线

输入目录不含 `.git`；源码配置显示基座版本 0.7.3，上游 commit 未确认。目标仓库初始提交为 `4090524`，内容只有 README。本地开发分支从该提交接续并导入源码，保留远端历史，使用普通非强制推送。

## 检查结果

Python 命令使用本机可用的 Python 3 运行时执行（对应仓库规范中的 `python3`）；Node 与 pnpm 使用本地运行时，在锁文件约束下安装依赖。未修改依赖版本。

| 命令 / 检查 | 结果 | 观察边界 |
|---|---|---|
| `python scripts/verify_engineering_contracts.py` | Passed | 工程契约静态检查 |
| `python -m unittest scripts.test_verify_engineering_contracts` | Passed，62 项 | 检查器自身行为 |
| `cd web && pnpm install --frozen-lockfile` | Passed | 锁定依赖安装 |
| `cd web && pnpm run lint:check` | Passed | 前端静态规范 |
| `node --test web/test/unit/chatStartScreen.test.js web/test/unit/database_create_flow.test.js` | Passed，5 项 | 页面模板与品牌标签回归 |
| `cd web && pnpm run test:unit` | Passed，333/333 | 首轮 331/333；旧品牌断言已显式更新，模板读取因 CRLF 失败已恢复 LF |
| `cd web && pnpm run build` | Passed | 生产资源生成；存在大 chunk 提示 |
| `cd docs && pnpm install --frozen-lockfile` / `pnpm run build` | Passed | 文档与站内链接；首次跨站点根链接已改为项目源码链接 |
| 新入口与计划相对链接检查 | Passed，24 个链接 | 文件存在性，非运行行为 |
| `docker compose exec api uv run --group test pytest test/unit -m "not slow"` | Not run | Windows 直接调用找不到 Docker；后续经 WSL 重试，因本项目尚未初始化安全配置且 api 未启动，测试未执行 |
| 本地 Vite `/login` 浏览器 DOM 与截图 | Inspected | 标题为 ClinEvidence，联系入口指向本项目；后端缺失显示连接失败，仅验证浅色错误状态 |
| 上传文件审查与常见密钥模式扫描 | Inspected | 无原始计划书、病历、本地 `.env`、运行产物或常见密钥命中；保留模板配置 |

ZIP 导入的 shell 脚本恢复 Git 执行位，`.gitattributes` 保持文本 LF，避免干净检出后脚本和源码模板测试受平台换行影响。独立 Reviewer 检查完整品牌补丁、源码导入、文档与规范；其提出的执行位、联系入口、上游快速开始引导及末尾空行问题均已修正。

## WSL Docker 验证

Windows 终端没有原生 Docker 命令。进一步检查发现已有 WSL Ubuntu 24.04、Docker Engine 29.6.0、Compose 5.1.4，Docker 服务处于 active，无需重复安装 Docker Desktop。已为当前 WSL 用户配置 Docker 访问权限，新终端可直接调用；容器使用者应了解 docker 组具有管理本地容器与宿主资源的权限。

- `wsl -d Ubuntu -- docker version`：客户端与服务端均可访问。
- `wsl -d Ubuntu -- docker compose version`：Passed，5.1.4。
- `wsl -d Ubuntu -- docker run --rm hello-world`：Passed，实际容器运行并退出。
- `wsl -d Ubuntu -- docker ps`：无运行中的项目服务。
- 在项目目录经 WSL 执行后端 unit 命令：Not run，Compose 解析因缺少 `SANDBOX_PROVISIONER_TOKEN` 失败；项目 `.env` 尚未初始化，未启动 api。Docker 可用不等于本项目已启动，M0 仍需配置模型、独立安全密钥并完成构建与运行验收。

在 PowerShell 中进入 Linux 开发环境：

```powershell
wsl -d Ubuntu
```

随后在 Ubuntu 中进入项目目录，按 README 初始化并使用 `docker compose`。仓库不包含本机凭据或 Docker 运行数据。

## 补丁检查

`git diff --check` 与 `git diff --cached --check` 检查实际上传补丁；首次源码导入的一处多余末尾空行已清理。

## 未验证范围

患者业务、医疗工具、快照时序、全病历质控与医疗评测未实现。原始病历、医院规则和测试题未提供，不能宣称临床或检索指标。原平台端到端运行须在 M0 单独验收。后端 unit、真实 HTTP/worker/SSE、正常登录、深色与窄屏完整业务均未验证；静态构建和登录错误页不能替代这些证据。
