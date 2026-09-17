# ClinEvidence 循证医助

基于 [Yuxi](https://github.com/xerrors/Yuxi) 二次开发的诊疗辅助与病历质控项目，用于秋招工程实践与演示。

**当前阶段：项目命名与开发计划。** 仓库包含 Yuxi 通用平台源码；患者工作区、医学证据问答与病历质控仍按计划开发，尚无医疗效果或性能实测结论。

## 开发阶段

完整任务、代码落点、产物与验收条件见 [ClinEvidence 秋招二次开发阶段计划](docs/develop-guides/clinevidence-roadmap.md)。按约 15 个全职工作日预算，依次推进基座验证、数据审计、患者与快照模型、PDF 解析、检索基线与优化、单 Agent、增量与上下文、异常恢复、全病历质控、Web 整合、冻结评测与面试交付。每阶段通过验收后再推进。

产品目标是患者原文与医学知识双通道证据问答，以及按完整病历和版本化规则生成可复算的质控报告。首期采用单 Agent、浏览器与手工导入；医生自行判断和修改正式病历，不接入 EMR 或执行医嘱。

## 基座与个人开发边界

| 内容 | 来源与状态 |
|---|---|
| Vue、FastAPI、LangGraph、知识库、图谱、权限、Run/FIFO、任务与审计 | 继承 Yuxi 源码；本机运行状态见验证记录 |
| ClinEvidence 品牌与本项目阶段计划 | 本仓库初始化改动 |
| 患者/就诊/快照、原文证据、时间隔离、医疗工具、质控与评测 | 计划开发，逐阶段验收 |

保留内部 `yuxi` Python 包、CLI、环境变量及存储标识，避免品牌重命名破坏现有契约。默认品牌由 [品牌模板](backend/package/yuxi/config/static/info.template.yaml) 提供；已有品牌覆盖文件按[品牌配置说明](docs/advanced/branding.md)调整。基座依赖版本为源码配置中的 0.7.3，输入目录不含 Git 历史，上游 commit 尚未确认。

## 启动基座

需要 Docker Engine、Docker Compose 和可用模型服务。以下为启动步骤，本机执行情况见[初始化验证记录](docs/develop-guides/clinevidence-bootstrap-validation.md)。

```bash
git clone https://github.com/yuhang-824/ClinEvidence.git
cd ClinEvidence
```

Windows PowerShell 初始化：

```powershell
.\scripts\init.ps1
```

Linux/macOS 初始化：

```bash
./scripts/init.sh
```

在本机填写初始化提示中的配置和凭据，再启动：

```bash
docker compose up --build -d
docker compose ps
curl --fail http://localhost:5050/api/system/ready
```

就绪后打开 [Web 工作区](http://localhost:5173) 初始化管理员；[API 文档](http://localhost:5050/docs)用于接口调试。首次验收使用合成资料。模型、OCR 与数据使用权限须单独确认。

## 开发与验证

- [架构地图](ARCHITECTURE.md)：实际服务边界与运行链路。
- [测试规范](docs/develop-guides/testing-guidelines.md)：测试层级和命令。
- [开发约定](AGENTS.md)：实现、证据与独立审查要求。
- [初始化验证记录](docs/develop-guides/clinevidence-bootstrap-validation.md)：已执行检查与未验证范围。
- [Yuxi 原项目](https://github.com/xerrors/Yuxi)：上游通用能力与历史资料。

源病历、题库、医院规则表、模型正文日志和 `.env` 不上传。公开演示仅使用合成或明确获准分发的资料，所有指标保留实际样本数与复现依据。

## 许可证与致谢

项目基于 Yuxi 开发，保留 [MIT License](LICENSE) 及 Yuxi Project Contributors 的版权声明。ClinEvidence 的规划与后续新增医疗业务应与上游已有平台能力分开介绍。
