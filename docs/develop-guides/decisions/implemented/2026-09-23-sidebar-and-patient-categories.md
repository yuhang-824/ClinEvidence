# 会话侧栏与患者库病种层级

状态：implemented
类型：feature
Owner：web/src/views/PatientLibraryView.vue

## 问题

会话侧栏历史记录过多；患者库直接平铺患者，患者主记录未保存病种，新增病种无法形成独立大类。

## 决策

“最近”会话默认显示 8 条，每次展开 8 条。患者库首页从可访问患者汇总病种大类，点击大类后显示该类患者，患者脱敏编号按中文自然顺序排列。没有分类的旧记录单列“未分类”，Owner 可以在患者详情中修正病种。

新建患者可选择已有病种或输入新病种；新病种随患者保存并出现在大类列表。会话中的快速创建入口遵循同一规则。患者 API 接受 1 至 32 字的病种，服务层去除首尾空白。PostgreSQL 的 `patients.category` 保存病种，storage-migrator 为旧库新增字段与通用文本约束，同时按明确的脱敏编号前缀归类旧患者；已有固定三值约束时先将其移除。患者可见性继续由 repository 查询控制，病种修改继续仅限 Owner。

## 替代方案

- 独立病种表及管理页面增加空病种、重命名和权限等独立状态；当前病种只随患者存在。
- 患者行后附病种标签无法提供先病种、后患者的导航层级。
- 固定三类加“其他”无法让新增病种形成自己的大类。

## 后果

最后一名患者删除或移出病种后，该大类从列表消失。内部导入仍可留下无法从编号确定病种的患者，需在“未分类”中处理。病种改名通过修改各患者记录完成。

## 验证

`docker compose run --rm storage-migrator` 完成 business schema 10 升级；数据库回读显示旧患者分类为内膜癌 16 人、宫颈癌 19 人、卵巢癌 17 人，约束为 `ck_patients_category_text`。迁移后 API 与 worker 健康。`docker compose exec web pnpm run lint:check` 与 `docker compose exec web pnpm run build` 均成功。登录后的真实页面交互未验证：独立浏览器会话只显示登录页。`uv run ruff check` 因容器内 editable 安装路径权限不足未完成。
