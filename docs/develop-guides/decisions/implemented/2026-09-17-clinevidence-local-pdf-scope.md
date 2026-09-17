# ClinEvidence 本地 PDF 知识库范围收窄

状态：implemented
类型：simplification
Owner：docker-compose.yml

## 问题

本记录面向 ClinEvidence 开发者，承接[分批精简提案](../proposed/2026-09-17-clinevidence-scope-reduction.md)的第一批。医院内网循证医助以本地 PDF 为知识来源，图谱、开放网页搜索与外部知识库连接器增加运行资源及维护面。

## 决策

Compose 移除 Neo4j 服务；后端移除图谱路由、任务、抽取及图谱增强检索，前端移除图谱展示和评估中的图谱选项。知识库工厂仅注册 Milvus，本地文件、文件夹和个人空间导入继续可用。Dify、Notion 连接器和 URL 抓取入口移除；`external_kb` 继续承担本项目知识库对 Agent/CLI 的 API，并非外部库导入器。

内置工具移除 Tavily、豆包联网搜索及通用深度研究预设，移除 MySQL 报表技能。Python 声明和锁文件移除 neo4j、networkx、langchain-tavily、tavily-python、pymysql；前端移除直接依赖 @antv/g6、d3，思维导图仍需要的传递依赖由锁文件管理。

知识库、Agent 和 Skill repository 负责旧配置的可见性与执行边界：旧外部库跳过初始化，退役预设及内置技能不可使用；历史记录和数据库字段保留。退役 slug 创建时使用可用后缀，技能名称唯一性仍检查历史记录。旧图谱待执行任务由任务发布器收敛为失败，避免永久排队。

## 替代方案

仅隐藏菜单无法消除后端调用和常驻服务成本。立即删除历史表与数据卷会扩大为数据迁移，因此采用跨层移除能力并保留历史数据。改换向量存储增加迁移风险，当前保留 Milvus、etcd、MinIO、PostgreSQL 和 Redis。

## 后果

现有部署停止 Neo4j，数据卷保留。PDF 解析、OCR、向量与混合检索、重排、评估、异步 worker 和来源阅读继续由现有模块承担。沙盒、MCP、模型适配器和追踪功能仍待后续取舍；移除内置联网搜索不等于已完成整个系统的网络隔离。

## 验证

旧能力不存在：范围单元测试验证图谱及 URL 抓取路由缺失、仅注册本地库、退役工具和技能不可执行；浏览器实际知识库仅显示文件管理、检索测试和评估，上传入口为文件、文件夹、个人空间。Neo4j 停止后 API 与 worker 正常启动。

`CLINEVIDENCE_SCOPE_SMOKE=1 uv run --no-sync pytest test/integration/api/test_clinevidence_local_pdf.py -q --show-capture=no` 通过。真实 HTTP、PostgreSQL、worker、Milvus 和对象存储完成合成 PDF 上传、后台解析索引、持久状态回读、检索、原文下载及删除。向量接口使用本地固定向量测试服务，仅证明协议和数据链路，未验证真实模型的语义检索质量、扫描 PDF OCR 或医疗回答正确性。真实数据库中合成的旧图谱任务经发布器处理后回读为 failed。

前端 lint、build 与 332 项 unit 通过；后端 90 项针对性 unit 通过。完整非 slow unit 首次运行 2062 passed、53 skipped、6 failed；其中 5 项范围变更后的断言已修正并通过针对性回归，剩余 XLS fixture 的 LibreOffice 转换失败，相关解析实现未修改。该失败不作为通过项。

工程契约检查、62 项契约 unit、文档构建及 `git diff --check` 通过。精简镜像重建在依赖字节码编译阶段耗时异常并影响本地响应，已主动终止；当前沿用原镜像运行修改后的源码，API 和 worker 运行容器已卸载上述五个 Python 包；重新创建容器前仍需完成镜像重建，不宣称新镜像构建完成。

重新引入条件：出现明确的循证需求并证明普通文档检索无法满足，通过独立提案恢复所需能力及其运行、权限和数据契约。
