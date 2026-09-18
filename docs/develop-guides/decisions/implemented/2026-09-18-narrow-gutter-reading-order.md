# 窄栏间距的 PDF 阅读顺序

状态：implemented
类型：bug-fix
Owner：backend/package/yuxi/knowledge/parser/pdf_layout.py

## 问题

文字层同行字符聚合采用固定最小 12 点间距。正文行末伸入栏间空白时，实际间隔约 11 点的左右栏文字被合成跨栏块，阅读排序据此提前输出右栏。第 5 节标题在左栏底部而正文在右栏顶部，结果正文被排到标题前并混入第 4 节。字符覆盖检查无法发现这种顺序错误。

## 决策

字符聚行和阅读排序共用空隙端点扫描，以支持行数最多的实际交集确认栏边界，避免短行或缩进使空隙中点偏移；确认后在该边界分开间隔较窄的文字，保留正常宽标题和表格处理。不使用标题内容或医学关键词重排，不自动改写已有审核稿或索引。以实际 PDF 第三页和独立合成 PDF 证明章节前后顺序与内容覆盖。

## 替代方案

全局降低字符间距阈值可能把普通单栏标题拆散；在切片阶段搬移医学正文会掩盖解析错误和破坏来源位置，均不采用。

## 后果

旧解析稿须重新解析才会获得正确阅读顺序，继续遵守审核后入库。此修复针对文字层窄栏间距，不声称任意扫描 OCR 框的跨栏关系都可自动恢复。

## 验证

- `docker compose exec -T api uv run --no-sync pytest test/unit/knowledge/test_pdf_layout.py test/unit/knowledge/test_mixed_chunking.py -q --show-capture=no -p no:cacheprovider`：65 passed。独立合成 PDF 包含全宽标题、10 点窄栏缝、左下标题和右上续文；固定坐标负例覆盖空隙中点错过真实栏缝。
- `docker compose exec -T -e CLINEVIDENCE_SCOPE_SMOKE=1 api uv run --no-sync pytest test/integration/api/test_clinevidence_local_pdf.py -q --show-capture=no -p no:cacheprovider`：1 passed（12.09 秒）。真实 HTTP/worker 解析两页合成 PDF，在原解析稿和索引数据库回读中断言标题先于右栏续文；Embedding 为确定性替身。
- 实际 PDF 第三页渲染与原稿对照：Inspected。八页重新解析后第 5 节的标题、定义、Spiliotis、MSKCC、CHIPOR 和推荐按序出现，对应片段 17–19。全文非空白字符多重集合与修复前一致，42 个片段覆盖所有正文字符，9 个推荐块保留。原材料及预览文件不提交仓库。
- 独立 Reviewer 复查固定案例及 20,000 组固定种子的双栏短行/缩进组合，无新增阻塞。该探针不替代正式 unit。
- Python lint 与格式检查通过；前端未修改。

- 最终完整后端 `pytest test/unit -m "not slow" --show-capture=no -p no:cacheprovider`：2149 passed、53 skipped、7 subtests passed（70.71 秒）。根工程约束检查、62 项 verifier unit、文档构建和 `git diff --check` 均通过。
