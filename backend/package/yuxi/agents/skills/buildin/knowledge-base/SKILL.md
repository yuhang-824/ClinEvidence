---
name: knowledge-base
slug: knowledge-base
description: "使用 Yuxi 知识库进行检索、打开文档、文档内定位和查看思维导图。当用户需要基于已配置知识库回答问题、核验资料或引用文档内容时使用此技能。"
---

# 知识库技能

当用户要求基于项目知识库、内部资料、上传入库文档或知识图谱相关内容回答问题时，使用此技能。

## 可用工具

- `list_kbs`：列出当前会话可访问且已启用的知识库。
- `query_kb`：按 `kb_id` 在指定知识库中检索内容，返回 `file_id` 和相关片段。
- `open_kb_document`：按 `kb_id` 和 `file_id` 打开文档原文窗口，适合查看更完整上下文。
- `find_kb_document`：在已知文档内用关键词或正则定位段落。
- `get_mindmap`：查看知识库思维导图结构。
- `search_file`：按文件名关键词搜索知识库中的文件，支持指定知识库或跨知识库，返回文件列表与分页信息。
- `download_kb_file`：按 `kb_id` 和 `file_id` 下载知识库原始二进制（pdf/docx/xlsx 等）到沙盒 `outputs` 目录，返回沙盒内可见的 `virtual_path`。当后续需要用代码读取原始文件结构（如 `openpyxl` 读 xlsx 单元格、`pdfplumber` 重新解析版面）时使用；`query_kb`/`open_kb_document` 只返回文本切片，无法满足需要文件对象的场景。

## 操作流程

1. 需要先确认当前会话有哪些知识库可用；不确定时调用 `list_kbs`。
2. 针对用户问题选择最相关的知识库，使用 `query_kb` 检索。
3. 如果检索片段不足以回答，使用返回的 `file_id` 调用 `open_kb_document` 查看上下文。
4. 如果用户要求定位术语、指标、章节或原文证据，使用 `find_kb_document` 在候选文档内查找。
5. 当用户关心知识库结构、文件分类或知识框架时，使用 `get_mindmap`。

## 回答必须标注引用

基于检索结果给出的每条论断，在该句末尾附上引用标记，写清依据来自哪个文件、哪一页：

```html
<cite source="文件名.pdf" data-page="12" type="file">1</cite>
```

- `source`：取片段里 `metadata.source` 的原值，不要改写文件名，也不要写知识库名称；
- `data-page`：取该片段 `metadata.source_metadata.pages` 里的页码；片段跨多页时用论断所在的那一页，判断不了就用第一个页码；
- 标签正文 `N`：本条回答里的引用序号，从 1 开始递增；同一片段重复引用沿用同一序号；
- 片段是历史片段或来自外部只读知识库、没有 `pages` 时省略 `data-page`，只写文件名。

示例：

```text
所有子宫内膜癌患者都应进行 Lynch 综合征筛查<cite source="1_1_第六期指南最终版1.0.pdf" data-page="12" type="file">1</cite>；若 MMR 蛋白表达缺失或为 MSI-H，建议遗传咨询<cite source="1_1_第六期指南最终版1.0.pdf" data-page="15" type="file">2</cite>。
```

只能引用检索结果里出现过的文件名和页码，禁止凭印象补全；没有检索依据的内容不要加引用标记。

## 关键约束

- 只能访问当前会话配置和用户权限允许的知识库。
- 不要编造 `kb_id` 或 `file_id`；优先从 `list_kbs` 和 `query_kb` 的返回结果中获取。
- Dify 等外部只读知识库可能只支持检索，不一定支持打开全文或文档内查找；遇到工具返回限制说明时，应如实告知用户。
