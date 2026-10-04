# CQU 制度知识库

本目录是 RAG 文档身份的根目录，递归导入 TXT、Markdown、PDF、DOCX、CSV 和 XLSX。`README` 是维护说明，不列入知识库或检索索引。

## 当前资料

唯一入库文件为 `cqu/finance/重庆大学财务报销指南-2025年版.pdf`，共 22 个物理页面，SHA-256 为 `4c571b93c55a3c086f1f24dcb16b12496765de41963df579403eeb97e7ef781e`。该文件是当前提供的 2025 版原文，保留经费适用条件、条款、表格、页码和标题路径。

已移除原企业差旅演示资料、重复格式和生成脚本。CQU 原文只维护这一份，不按问答主题另存金额表或复制文件。测试评测题位于 `tests/data/golden_queries.json`，不参与入库。

## 检索与更新

当前部署使用 `HOMMEY_RAG_SEARCH_SCOPES=cqu/finance`；向量召回和 BM25 按相同目录前缀筛选，查不到时不扩大范围。空配置表示全库检索。上传文件进入配置中的第一个分区，分区控制不等于用户访问权限。

更新时将核实过的 CQU 文件放到对应目录，再通过管理员知识库页面执行刷新，或运行：

```powershell
docker exec hommey-app python scripts/enqueue_rag_refresh.py
```

刷新会重建当前 collection 的活动索引，移除已删除文件的检索片段；仅删除源文件不能清除已有索引。刷新成功后核对来源、页码、经费条件和入库状态。

后续可加入 `cqu/research` 等目录，刷新后配置 `HOMMEY_RAG_SEARCH_SCOPES=cqu/finance,cqu/research` 并重启应用。文档 ID 始终使用相对于本目录的路径。
