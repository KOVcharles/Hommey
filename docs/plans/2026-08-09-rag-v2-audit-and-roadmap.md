# Hommey RAG V2 现状审计与改造路线图

> 审计日期：2026-08-09<br>
> 审计范围：知识库上传、文档解析、OCR、规范化、切块、向量化、混合检索、查询改写、回答生成、引用与运维<br>
> 本轮性质：只读审计与方案设计；未修改生产运行逻辑、索引或知识库原文

## 1. 结论先行

当前 RAG 已具备可运行的基础闭环：管理员上传 TXT/Markdown/PDF，后台全量重建，文本向量与 Python BM25 混合召回，经 RRF 和少量领域规则排序，再由 `ask-question` 生成受知识库约束的回答。全量重建已有蓝绿切换和失败保留旧索引机制，这是现阶段最可靠的一部分。

但它仍属于“纯文本、小规模、单领域的第一版 RAG”，尚未形成成熟的文档理解系统。最主要的问题不是缺少 HyDE，而是索引前的数据结构和切块质量不足：所有格式最终都被压成一个字符串并进入同一切块器，标题、表格、列表、页面版面和 OCR 信息没有得到保留。若直接叠加 HyDE，会提高召回调用量，却不会修复错误切块、扫描页丢失和证据污染，反而可能放大噪声。

综合判断：

- 未发现会立即破坏现有知识库的 S0 级灾难性缺陷；全量刷新失败时旧索引能保留。
- 确认存在 5 类高优先级风险：扫描 PDF 内容静默丢失、切块严重碎片化、增量入库重复、无通用相关性门槛导致弱证据进入回答，以及 RAG Agent 异常状态与 Skill 输出 schema 不一致。
- 当前并非真正“只有 query 改写”：底层还有向量 + BM25 + RRF；但所谓改写主要是上游 `rewritten_query`、去口语化和极少数硬编码扩展，没有统一的查询规划、HyDE、通用重排或完整检索追踪。
- PDF OCR 目前不是“部署不合理”，而是根本未部署。现有 `pypdf` 只能提取已有文本层，不能识别扫描图像。
- 表格当前既没有知识库上传支持，也没有结构化索引。聊天附件虽能读 DOCX 表格，但会破坏正文与表格的原始顺序，而且不会进入长期知识库。
- 推荐先完成结构化解析、格式感知切块、去重和评测基线，再以功能开关方式上线条件式 HyDE。

## 2. 当前链路全景

```text
管理员知识库
TXT / MD / PDF
      │
      ▼
FileSystemDocumentLoader
      │
      ▼
TxtParser / PdfTextParser(pypdf)
      │
      ▼
ParsedDocument(text)              聊天附件（另一条链路）
      │                           TXT / MD / DOCX / PDF
      ▼                                   │
TextNormalizer                           ▼
      │                           DocumentProcessor
      ▼                                   │
TextChunker(600 字符/100 重叠)            ▼
      │                           最多 12000 字符注入请求
      ▼                           不进入知识库索引
Embedding API
      │
      ▼
Milvus Lite dense vector + Python 全量扫描 BM25
      │
      ▼
RRF + 餐费领域硬编码规则
      │
      ▼
top_k=3 → ask-question → LLM 总结 → sources
```

关键代码入口：

- 配置：`rag/config.py`
- 加载：`rag/loader.py`
- 解析：`rag/parser.py`
- 规范化：`rag/normalizer.py`
- 切块：`rag/chunker.py`
- 入库编排：`rag/pipeline.py`
- 存储和混合检索：`rag/milvus_store.py`
- Embedding：`rag/embedder.py`
- RAG Agent：`.agents/skills/ask-question/script/agent.py`
- 知识库管理：`webui_new/knowledge_base_service.py`
- 聊天附件解析：`multimodal/document_processor.py`

## 3. 成熟度评估

| 能力 | 当前等级 | 结论 |
|---|---:|---|
| 上传与文件安全 | 2.5/5 | 有后缀、大小、空文件和同名保护，但文件类型少，PDF 只校验 `%PDF` 文件头 |
| 文档解析 | 1.5/5 | TXT/MD/PDF 文本层可用，没有统一文档元素模型、版面、表格或 OCR |
| 切块 | 1/5 | 单一字符切块器；当前语料严重碎片化，不按格式和 token 处理 |
| 索引一致性 | 2.5/5 | 全量蓝绿刷新可靠；增量写入没有去重，版本元数据不足 |
| 检索 | 2.5/5 | 已有 dense + BM25 + RRF；BM25 不可扩展，规则仅覆盖餐费，缺少通用重排和门槛 |
| Query 理解 | 2/5 | 有去口语化和局部多查询，但无统一 QueryBundle、HyDE、实体/范围约束 |
| 回答约束 | 3/5 | 提示词对证据约束和提示注入防护较好；缺少 claim-citation 校验 |
| 引用与可观测性 | 1.5/5 | 有文件、页码和 section 字段，但 section 基本不真实，缺少 bbox、excerpt 和完整评分链路 |
| 评测与回归 | 1.5/5 | 单元测试可用，但无黄金查询集、Recall/MRR/nDCG、OCR/表格质量评测 |
| 部署与扩展 | 2/5 | Milvus Lite 适合当前小语料；OCR/解析任务仍在 Web 进程线程中，不适合重任务 |

### 3.1 本次进程解析到的实际配置

| 配置 | 当前值 |
|---|---|
| Embedding backend | `siliconflow` |
| Embedding model | `BAAI/bge-large-zh-v1.5` |
| Embedding dimension | `1024` |
| Chunk size / overlap | `600 / 100` 字符 |
| Final / vector / BM25 top-k | `3 / 10 / 10` |
| Knowledge file types | `txt, md, pdf` |

代码 dataclass 的模型默认值是 `BAAI/bge-m3`，本次环境实际解析为 `BAAI/bge-large-zh-v1.5`。环境覆盖本身没有问题，问题是 manifest 没有保存最终生效值，因此仅查看索引无法知道它由哪个模型生成。

### 3.2 真实知识库检索抽样

以下为本轮对现有 Milvus 知识库的只读查询观察，不代表完整评测：

| 查询 | 结果概况 | 暴露的问题 |
|---|---|---|
| 北京出差住宿费标准是多少 | 第 1 名正确命中北京 500 元条款，但 top-3 混入其他城市 300/400 元条款 | 城市范围缺少 metadata filter，回答模型承担二次辨别风险 |
| 我出差有餐补吗 | FAQ 与餐费条款靠前 | 餐费硬编码规则有效，但不可泛化 |
| 出差期间生病的医疗费能报销吗 | 真正相关 FAQ 位于第 3 名，第 1 名是独立文档标题 | 标题碎片污染排序 |
| 发票丢了怎么报销 | top-3 未命中“遗失发票”条款 | 口语同义召回失败，且无 evidence gate |
| 高铁可以坐什么座位 | 前两名正确，第 3 名混入飞机公务舱 FAQ | top-k 中存在跨费用类型噪声 |
| 2025年公司的差旅碳排放目标 | 第 1 名正确，第 2 名是独立章节标题 | 标题块重复占用证据槽位 |

这组抽样说明当前系统不是完全不可用：精确术语和餐费领域已有较好结果；主要风险集中在同义表达、范围约束、标题碎片和弱相关结果拒绝。

## 4. 已确认的 Bug 与风险

### 4.1 S1：扫描 PDF 和混合 PDF 页面会静默丢失

现状：`PdfTextParser` 对每页调用 `pypdf.page.extract_text()`。返回空字符串的页面被直接 `continue`，不会生成 `parse_error`，不会标记需要 OCR，也不会记录“本页未索引”。

影响：

- 纯扫描 PDF 会生成 0 个页面；全量刷新在没有其他有效文件时失败，但无法告诉管理员真实原因是需要 OCR。
- 混合 PDF 更危险：有文字层的页面正常入库，扫描页悄悄消失，刷新仍可能显示成功。
- manifest 会把未报错的源文件标为 `indexed`，造成“界面显示已索引，但部分页不可检索”的假成功。
- 聊天附件的 PDF 解析同样会把空页和异常页静默跳过。

修改方向：每个原始页面必须得到一个终态：`native_text`、`ocr_text`、`intentionally_skipped` 或 `error`。任何页面都不能无记录消失。

### 4.2 S1：当前切块严重过碎，标题成为无意义独立证据

当前配置是 600 字符、100 字符重叠，但真实语料并未接近这个上限。对现有 `data/documents` 进行不写库重算：

| 指标 | 实测值 |
|---|---:|
| 源文件 | 10 |
| 生成块 | 365 |
| 最短 | 4 字符 |
| P25 | 46 字符 |
| 中位数 | 70 字符 |
| P75 | 108 字符 |
| 最长 | 358 字符 |
| 小于 100 字符 | 257 / 365（70.4%） |

典型碎片包括“二、交通标准”“三、住宿标准”“十、附则”。原因是切块器遇到章节标题立即冲刷当前块，下一条编号子标题再次冲刷，导致章节标题单独成块。实际在线检索中，“出差期间生病的医疗费能报销吗”的第 1 名就出现了仅含“差旅费用报销规定”的标题块，而真正 FAQ 位于第 3 名。

修改方向：标题不应单独作为叶子证据，应作为 `heading_path` 前缀附加到其后正文；短小相邻条款应合并，超长条款才在句子或 token 边界拆分。

### 4.3 S1：增量入库没有去重，会重复写入相同 chunk

`add_chunks()` 直接按当前行数追加，未按 `hash`、`source_path + logical_chunk_id` 或文档版本 upsert。实测同一 TXT 文件连续两次 `rebuild=False`：第一次新增 1 块，第二次仍新增 1 块；两行 hash 完全相同。

影响：

- CLI 或未来增量上传重复执行会制造重复召回。
- 重复块占据 top-k，使证据多样性下降。
- 并发增量写入都基于 `current_count` 计算主键，存在 ID 冲突窗口。

当前 Web 管理页使用全量 `rebuild=True`，所以日常刷新暂时绕开了该问题，但公共 pipeline 明确支持增量，不能把它视为安全接口。

修改方向：以稳定 chunk hash 为幂等键；文档更新采用“新版本写入 → 校验 → 按 doc_id 切换/删除旧版本”，而不是 append。

### 4.4 S1：缺少通用相关性门槛，弱相关结果仍会成为回答证据

`hybrid_search()` 始终从向量和 BM25 取排名结果，RRF 后直接返回 top-k；没有经过语料标定的相似度阈值、通用 reranker 阈值或 no-knowledge 判定器。`filter_relevant_results()` 只在餐费领域产生 term，且仍无条件保留 vector rank 1 和 BM25 rank 1。

实测查询“发票丢了怎么报销”未命中语料中的“遗失发票”条款，前三名分别偏向发票验真、无关标题、超标准金额。说明当前系统对同义表达缺乏稳定召回，却仍会把不充分结果送给回答模型。

修改方向：建立通用重排器和 `evidence_gate`，校准“可回答 / 部分依据 / 无依据”；原始排名第一不再等于强制有效证据。

### 4.5 S2：并未按 Markdown、表格或 PDF 结构切块

`TxtParser` 同时处理 TXT 和 MD，Markdown 的 `#` / `##` 标题不会触发当前主题规则。诊断样例中的两级 Markdown 标题和两段正文被合成同一个块。当前主题规则只识别：

- `Q数字:`
- `一、标题`
- `1. 标题`，且点号后必须有空白

它不识别 Markdown heading、`1、`、`（一）`、`第X章`、列表、代码块、表格、引用块或 PDF 版面元素。

### 4.6 S2：规范化会破坏表格列对齐和代码缩进

`TextNormalizer` 会将每行连续空格和 tab 压为一个空格，并对每行 `strip()`。例如：

```text
城市    职级    上限（元）
北京    高级    500
```

会变成：

```text
城市 职级 上限（元）
北京 高级 500
```

这对普通段落无害，但会丢失定宽表格、代码块、缩进列表和 OCR 版面信息。表格接入前必须改为按 block type 规范化。

### 4.7 S2：PDF 的 `chunk_index` 和 `chunk_id` 不稳定

Pipeline 对每个规范化页面分别调用 `chunker.chunk([document])`，因此 `chunk_index` 在每页重新从 1 开始。Milvus metadata 又将 `chunk_id` 写为 `{文件名 stem}_{chunk_index}`，多页 PDF 会产生重复 `chunk_id`。hash 因包含页码仍不同，但对日志、引用和未来 upsert 不可靠。

修改方向：采用稳定 ID，例如：

```text
document_id / document_version / page_number / block_id / chunk_ordinal
```

### 4.8 S2：解析与索引版本不可追溯

当前 `ingestion_manifest.json` 只有：

- `refreshed_at`
- 文档 `sha256` 和 `indexed_at`
- 汇总 report

没有 `schema_version`、parser/chunker 版本、OCR 模型、embedding 模型与维度、query instruction、索引配置或代码版本。改变 embedding、切块或解析算法后，系统无法判断是否必须重建，也无法复现某条回答使用的索引版本。

### 4.9 S2：当前 BM25 每次查询扫描全部文档

`bm25_search()` 每次查询都：

1. 按整数 ID 范围从 Milvus 拉取全部块；
2. 重新 tokenize 全部文档；
3. 重新统计 DF 和 BM25；
4. 在 Python 中排序。

365 块时尚可，扩展 DOCX/XLSX/PDF/OCR 后会随语料线性恶化。`fetch_all_documents()` 还假定 ID 从 1 到 count 连续，未来删除或 upsert 后会漏数据。

Milvus 官方已支持 BM25 sparse 和 dense/sparse hybrid，但官方集成说明 Milvus Lite 的内建 full-text 能力存在部署版本限制。因此应先核实当前 `pymilvus/milvus-lite` 组合；若 Lite 不支持，选择预计算 BM25 索引或升级为 Milvus Standalone，而不是继续逐查询全表扫描。

### 4.10 S2：重排规则几乎只覆盖餐费领域

`_rerank_terms()` 和 `_off_topic_penalty()` 主要识别餐补、餐费、早午晚餐、酒水等词。住宿、交通、审批、发票、医疗、国际差旅和城市范围没有通用重排逻辑。变量名虽叫 rerank，实际上不是 cross-encoder 或通用语义重排器。

此外，内部产生的 `rerank_score` 没有进入 `RetrievalResult` 和 `to_dict()`，日志和调用方看不到最终排序依据。

### 4.11 S2：Embedding 配置和查询方式缺少契约

当前 manifest 不保存实际 embedding 模型和维度；本地 SentenceTransformer 路径逐条 encode，未批量处理。若使用 BGE 中文 v1.5，官方模型卡对短查询检索推荐 query instruction，语料 passage 不加 instruction；当前 `embed_query()` 与 `embed_texts()` 完全同路，未形成可配置的 query/passage 编码契约。

这不是断言“当前结果必然错误”，但应通过离线评测决定是否启用 instruction，并把选择写入索引版本。

### 4.12 S2：引用只能到文件/页，无法稳定指向真实章节和原文位置

`ask-question` 希望保留 file/page/section/excerpt，但当前 chunk 没有真实 section；`title` 通常是文档或 PDF 页的第一行。输出 schema 对 source item 没有严格字段约束，也没有 excerpt、block_id、bbox、table_id/cell range。

结果是“看似有引用”，却无法可靠完成审计回溯和页面高亮。

### 4.13 S1：RAG Agent 的异常输出违反 Skill schema

`.agents/skills/ask-question/schemas/output.json` 只允许 `status=success|no_knowledge`，并要求 `status` 和 `answer` 必填；但 Agent 实际还会返回：

- 初始化失败时：`status=error`，只有 `message`，没有必填的 `answer`；
- 知识库为空时：`status=knowledge_base_empty`。

这属于明确的接口契约 Bug。只要编排器对 Skill 输出执行 schema 校验，最需要稳定处理的异常路径反而可能被判为无效输出，进而触发二次 fallback 或掩盖原始错误。

修改方向：统一状态机和 schema，至少定义 `success | partial | no_knowledge | knowledge_base_empty | error`；使用 JSON Schema 条件约束各状态必填字段，并为每个异常分支加入端到端验证。

### 4.14 S3：文本编码策略不一致

知识库 TXT/MD 强制 UTF-8，聊天附件则依次尝试 UTF-8 BOM、UTF-8、GB18030、GBK。相同文件在聊天中可读，在知识库上传时可能被拒绝。应统一策略，并在管理界面明确展示检测结果；不建议用 `errors=ignore` 静默吞字符。

### 4.15 S3：Embedding API 没有重试与退避

当前有超时、响应数量和维度校验，这是好的；但没有针对 429/5xx/瞬态网络错误的有限重试、指数退避和批次级恢复。全量蓝绿机制能保旧索引，但一次瞬态错误会让整次刷新失败并重新计算全部向量。

## 5. 文档格式支持现状

| 格式 | 知识库上传 | 知识库解析 | 聊天附件 | 结构化切块 | 主要问题 |
|---|---|---|---|---|---|
| TXT | 支持 | UTF-8 纯文本 | 支持多编码 | 否 | 仅按空行/少量标题正则 |
| Markdown | 支持 | 当作 TXT | 当作 TXT | 否 | 标题、列表、代码、表格语义丢失 |
| PDF 文字层 | 支持 | `pypdf` 按页抽字 | 支持 | 否 | 无版面、表格、页眉页脚处理 |
| PDF 扫描件 | 表面支持 | 不支持 | 不支持 | 否 | 页面静默为空 |
| DOCX | 不支持 | 不支持 | 支持 | 否 | 附件中段落和表格顺序被重排 |
| CSV | 不支持 | 不支持 | 不支持 | 否 | 无 schema/type/unit 识别 |
| XLSX | 不支持 | 不支持 | 不支持 | 否 | 无 sheet、合并单元格、公式处理 |
| HTML | 不支持 | 不支持 | 不支持 | 否 | 无 DOM/标题/表格处理 |
| 图片 | 不支持 | 不支持 | 不支持 | 否 | 无 OCR |
| PPTX | 不支持 | 不支持 | 不支持 | 否 | 可放后续阶段 |

## 6. 目标数据模型：先保留结构，再决定如何切块

不应继续让所有解析器只返回一个 `text`。建议建立中间文档模型：

```text
ParsedDocument
├── document_id / version / sha256
├── filename / file_type / mime_type
├── parser_name / parser_version
├── pages[]
│   ├── page_number / width / height
│   ├── parse_mode(native|ocr|mixed)
│   ├── ocr_confidence / warnings[]
│   └── blocks[]
│       ├── block_id
│       ├── type(title|heading|paragraph|list|table|code|image|footer...)
│       ├── text
│       ├── heading_path[]
│       ├── bbox / reading_order
│       └── table_data / cells / row_span / col_span
└── metadata
    ├── category / department / jurisdiction
    ├── effective_date / expiry_date / policy_status
    └── acl / tenant_id
```

最终 chunk 应至少包含：

```text
chunk_id, chunk_hash, document_id, document_version,
page_start, page_end, block_ids, heading_path,
content_type, retrieval_text, display_text,
parser_version, chunker_version, embedding_version,
source offsets/bbox, policy metadata
```

其中 `retrieval_text` 可以带标题路径、表头和同义标签，`display_text` 必须保持可引用的原始内容，两者不能混为一谈。

## 7. 按格式切块的建议

### 7.1 TXT/普通政策文本

- 识别多种中文法规层级：`第X章`、`一、`、`（一）`、`1.`、`1、`、`Q1:`。
- 使用 heading stack，将上级标题附加到每个正文块。
- 短标题必须与至少一个正文/子条款合并，不允许 4～10 字标题单独入库。
- 长条款优先按句号、分号、列表项边界拆分，最后才使用字符窗口。
- chunk size 改为 tokenizer 计数；建议先试 220～420 tokens，overlap 40～80 tokens，通过评测确定，不把参数写死成“行业最佳值”。

### 7.2 Markdown

- 使用 Markdown AST，不使用纯正则。
- 按 heading hierarchy 分节，保留 `heading_path`。
- 列表项保持完整；列表过长时以若干项为一块并重复列表标题。
- fenced code block 不从中间切断；代码与解释可分别索引并建立相邻关系。
- Markdown table 进入表格处理，不与普通段落一起压空格。

### 7.3 DOCX

- 必须按 OOXML body 顺序遍历 paragraph/table，不能先提取所有段落再追加所有表格。
- Heading 样式映射为层级；列表保留编号和缩进。
- 表格保留合并单元格、表头和所在章节。
- 页码在 DOCX 中通常不可靠，可使用 section/block 定位，不伪造页码。

### 7.4 CSV/XLSX

- 每个 sheet 单独建立 `sheet_name` 和表格 schema。
- 识别表头、单位、数据类型、空值、合并单元格、公式和显示值。
- 对宽表按列组，对长表按连续行窗口切块；每块重复文档标题、sheet 名和列头。
- 不把整张大表转成一个超长 Markdown 字符串。
- 对政策标准表同时建立三种表示：结构化 table JSON/HTML、行级检索文本、表级摘要块。
- 精确金额问答优先引用结构化单元格；摘要只用于召回，不能替代原始值证据。

表格行级检索文本示例：

```text
章节：国内住宿标准
表：一线城市住宿上限
列：城市=北京；职级=高级；上限=500元/晚；生效日期=2026-01-01
```

### 7.5 文字型 PDF

- 原生文本提取作为快路径，但要保留 word/block bbox 和 reading order。
- 识别并去除重复页眉、页脚、页码水印；原始 block 仍保留用于审计。
- 两栏/多栏按版面阅读顺序重排。
- 表格区域进入 table parser，不用普通 `extract_text()` 的空格结果。
- 文档标题保持全局一致，页面第一行不能覆盖 document title。

### 7.6 扫描 PDF 与图片

- 先做页面级分类，不对所有 PDF 强制 OCR。
- OCR 后保留 page、bbox、reading order、置信度、语言和模型版本。
- OCR 低置信度、超时、加密、损坏、跳过页都应显式写入 ingestion report。
- OCR 文本不能只保存拼接结果；需要保留 block 和坐标，支持引用回原图。

## 8. OCR 部署建议

### 8.1 当前部署事实

- `requirements.txt` 只有 `pypdf` 和 `python-docx` 相关解析依赖。
- Docker 镜像只安装 `curl` 和 `libpq5`，没有 Tesseract、Poppler、PaddleOCR、Docling 或 OCRmyPDF。
- 知识库刷新由 Web 进程内 daemon thread 执行；适合当前轻量解析，不适合长 PDF、CPU/GPU OCR 和大模型版面分析。

因此当前 OCR 状态应明确标为 `not_configured`，而不是继续让扫描 PDF 显示“已索引”。

### 8.2 推荐架构

```text
上传 → 文件安全检查 → 解析任务队列 → 页面探测
                                      ├─ 原生文本质量合格 → layout-native parser
                                      ├─ 原生文本不足 → OCR/layout worker
                                      └─ 加密/损坏/超限 → 显式失败
                                                    │
                                                    ▼
                                  结构化中间文档 + 质量报告
                                                    │
                                                    ▼
                                  chunk/index staging → 校验 → 原子发布
```

建议采用“轻量快路径 + 可插拔重解析器”：

1. 原生 PDF 继续走快速文本/版面抽取。
2. 仅对低文本密度、图片型或表格复杂页面调用 OCR/layout worker。
3. 中文扫描件与复杂表格优先做 PaddleOCR PP-StructureV3 小规模 PoC；它的官方文档覆盖版面、表格、公式和多类文档区域。
4. 若更看重统一支持 PDF、DOCX、XLSX、PPTX、HTML、CSV，可评估 Docling 作为统一转换器；其 lossless JSON/HTML 更适合保留合并单元格，不能只使用会压平 span 的 Markdown 导出。
5. OCRmyPDF 适合生成可搜索 PDF 或重做 OCR 层，但它不是表格结构解析器，不建议单独承担 RAG 文档理解。

最终选择应由一组真实中文制度 PDF、扫描件、无框表格、合并单元格和多栏文件进行准确率/耗时/资源占用对比后决定，不应一次性把所有重依赖装进主 Web 镜像。

### 8.3 页面级 OCR 路由初始规则

下列仅作为待校准初值：

- `native_text_chars < 40`：进入 OCR 候选。
- 可打印字符比率异常、乱码/替换字符过多：进入 OCR 候选。
- 页面有大面积图片且文字层覆盖明显不足：区域 OCR。
- 原生文本正常但检测到复杂表格：仅对表格区域做结构识别。
- 加密 PDF：返回 `password_required`，不静默失败。
- OCR 超时或低置信度：页面状态为 warning/error，管理员可查看并重试。

不要仅用“是否有任意文字”决定跳过 OCR。水印、页码或错误 OCR 层会造成假阳性。

### 8.4 部署隔离

- 建立 `document-worker`，与 Uvicorn Web 进程分离。
- 队列任务具备幂等 `job_id + document_sha256 + parser_version`。
- 配置单文件页数、像素、解压大小、执行时间、并发、CPU/GPU 和临时磁盘上限。
- 解析产物先写 staging，质量校验完成后再发布索引。
- OCR/layout 作为 optional dependency 和 feature flag；关闭时清晰返回“不支持扫描件”，不假装成功。

## 9. HyDE 设计：应作为受控召回分支，而不是替换原查询

HyDE 的原始论文流程是：LLM 生成一段假想相关文档，再将其编码为向量，用该向量在真实语料附近检索。论文同时明确指出假想文档可能包含错误细节。对公司政策尤其是金额、审批和报销资格，这一风险必须隔离。

### 9.1 推荐 QueryBundle

```text
QueryBundle
├── original_query             → dense + lexical
├── normalized_query           → dense + lexical
├── deterministic_variants[]   → dense + lexical（有限、可解释）
└── hyde_passage               → dense only
```

候选结果按稳定 chunk ID 去重，再使用可配置加权 RRF 融合。例如初始实验权重可从以下值开始，通过黄金集调参：

| 分支 | 初始实验权重 |
|---|---:|
| 原始 query dense | 1.0 |
| 原始/规范化 query lexical | 1.0 |
| 规范化 query dense | 0.9 |
| 确定性同义变体 | 0.8 |
| HyDE dense | 0.5～0.7 |

这些不是固定生产参数，必须以 Recall@k、MRR、no-knowledge precision、延迟和成本验证。

### 9.2 强制安全规则

- HyDE 只生成检索向量，绝不进入 BM25 查询。
- HyDE 文本绝不作为证据、引用或回答上下文。
- 最终回答只能引用真实 chunk。
- HyDE prompt 禁止主动创造金额、日期、城市等级、审批条件；即使如此仍按不可信合成数据处理。
- HyDE 超时、限流或模型错误时无损降级到原始检索。
- trace 中标记 `synthetic=true`，但日志要遵守敏感数据策略。

### 9.3 条件式启用

适合启用：

- 用户表达模糊、口语化，词面与政策术语差异大。
- 问题描述的是情境或后果，而不是精确条目名称。
- 基础 dense + lexical 的候选一致性或重排置信度低。

建议跳过：

- 精确政策编号、文件名、金额、日期、城市或航班/车次查询。
- 只有 1～2 个实体词且意图不完整。
- 原始查询已高置信命中精确条款。
- 非公司差旅领域、权限不足或输入安全校验失败。

“发票丢了怎么报销”是适合比较 query rewrite 与 HyDE 的样本，但更便宜、更可解释的第一步是将“丢了”规范化为“遗失/丢失发票、票据遗失处理”。只有基础改写仍低置信时才启用 HyDE。

### 9.4 HyDE prompt 草案

```text
你只生成用于语义检索的“假想公司制度片段”，不得回答用户。
保留用户问题中的地点、费用类型、日期、例外条件和否定词。
使用公司差旅制度常见术语描述可能相关的条款主题。
不得编造具体金额、比例、时限、审批人或报销结论；未知处使用抽象表述。
输出 80～180 个中文字符，不含解释、引用或指令。
```

### 9.5 缓存与追踪

- 缓存键：`normalized_query + model + prompt_version + tenant/policy_scope`。
- 记录每个 query variant、耗时、候选 ID、dense/BM25/RRF/rerank score、过滤原因和最终证据。
- 当前 `rerank_score` 会在数据模型转换时丢失，应先修复。
- 设定最大 query variant 数和总检索预算，避免多查询与 HyDE 乘法膨胀。

## 10. 检索层修改方向

### 10.1 优先于 HyDE 的修复

1. 删除标题孤块，重建结构化 chunk。
2. 建立稳定 ID、去重与版本化索引。
3. 建立黄金查询集，固化当前 baseline。
4. 引入通用 reranker 或最小可用 evidence classifier。
5. 建立 no-knowledge 阈值并按领域/模型校准。
6. 为 exact entity、金额、日期、否定词和适用范围增加 metadata/filter 约束。

### 10.2 混合检索

- 原始 query 必须始终保留 lexical 分支，以保护精确术语、文件编号、金额和地名。
- 中文 lexical tokenizer 不应只依赖逐汉字 + 少量硬编码短语；可采用可版本化的中文分词和领域词典。
- 语料扩大前，将 BM25 从逐查询全表计算迁出。
- 候选融合后再用通用 cross-encoder rerank；可以先评估 BGE reranker 系列，但模型选择必须以本地制度数据评测为准。
- 用 MMR/near-duplicate 过滤避免同一条款的重复 chunk 占满 top-k。
- 多事实问题应动态决定 evidence 数量，不应始终 `top_k=3`。

### 10.3 范围和时效过滤

政策类 RAG 应至少支持：

- 国内/国际、国家、城市和地区；
- 员工等级/部门/合同主体；
- 费用类型；
- 生效日期、失效日期、当前状态；
- 公开范围、tenant 和 ACL。

检索前从问题中抽取 filter，检索后仍校验范围冲突。不能只靠 LLM 阅读三个文本块自行判断。

## 11. 回答与引用框架修改方向

建议将 RAG 输出收紧为：

```json
{
  "status": "success | partial | no_knowledge | error",
  "answer": "...",
  "claims": [
    {
      "text": "...",
      "evidence_ids": ["chunk-id"]
    }
  ],
  "sources": [
    {
      "chunk_id": "...",
      "file": "...",
      "page": 3,
      "section": ["住宿标准", "一线城市"],
      "excerpt": "...",
      "bbox": [0, 0, 0, 0],
      "table": {"sheet": null, "table_id": null, "row": null}
    }
  ],
  "retrieval_trace_id": "..."
}
```

生成后增加 deterministic 校验：

- 每个数值、日期、比例、交通等级和“可/不可报销”结论必须关联至少一个真实 evidence ID。
- 引用的 evidence 必须属于当前检索结果且通过权限过滤。
- 回答中的地区/国内国际范围不得与证据 metadata 冲突。
- 无足够证据时返回 `partial` 或 `no_knowledge`，不靠措辞掩盖证据缺口。

## 12. 分阶段改造路线

### Phase 0：建立可比较基线

- 建立不少于 100 条黄金查询，覆盖精确问法、口语同义、否定、城市范围、多事实、无答案和表格问题。
- 保存当前 365 chunk 的 baseline 检索结果与回答结果。
- 增加 Recall@5/10、MRR@10、nDCG@10、no-knowledge precision、citation coverage 和 p50/p95 延迟。
- 在 manifest 中加入 schema/parser/chunker/embedding/index 版本。
- 增加检索 trace，保留所有分支评分和过滤原因。

完成条件：任何后续方案都能与同一数据集进行 A/B，而不是凭主观感受上线。

### Phase 1：先修数据质量和幂等性

- 引入结构化 block 模型。
- 完成 TXT/Markdown 格式感知切块。
- 修复标题孤块、PDF chunk ID、增量去重和 manifest 假成功。
- 统一文本编码策略。
- 增加 `parser_version/chunker_version` 触发重建。
- 对当前语料重建 V2 staging collection，通过回归后再原子切换。

完成条件：相同文件重复增量入库新增 0 块；没有短标题孤块；所有页面有明确状态。

### Phase 2：扩展 DOCX、CSV、XLSX 和表格

- 知识库上传、后端 parser registry、前端提示和预览统一开放格式。
- DOCX 保持 paragraph/table 原始顺序。
- CSV/XLSX 形成 sheet/table/cell 结构和三重索引表示。
- 表格引用可定位到 sheet、table、row/cell。
- 对公式、合并单元格、空表头、超宽/超长表建立边界测试。

完成条件：表格数值问答能够引用原始单元格，不依赖表级摘要生成数值。

### Phase 3：OCR 与版面解析

- 独立 document worker 和任务状态表。
- 原生文字快路径 + 页面级 OCR/layout fallback。
- 使用真实中文制度文档对 PaddleOCR PP-StructureV3 与 Docling 做 PoC。
- 显式记录空页、低置信度、超时、密码和损坏状态。
- 对页眉页脚、多栏、旋转、印章、水印、无框表格建立回归集。

完成条件：扫描页要么可检索，要么在管理端显示具体失败原因；不得静默消失。

### Phase 4：QueryBundle、通用重排和条件式 HyDE

- 将当前零散改写收敛到 QueryPlanner。
- 原始、规范化、同义变体、HyDE 分支可独立开关。
- HyDE 只走 dense，结果去重后加权融合。
- 接入通用 reranker 和 evidence gate。
- 为精确查询、模糊查询分别测量收益和退化。

完成条件：HyDE 在困难语义查询上有稳定增益，且不会显著降低精确政策查询、no-knowledge 和地区范围判断。

### Phase 5：检索基础设施扩展

- 迁移 Python 全量 BM25；根据当前 Milvus 版本验证 native sparse，或使用独立 lexical index。
- 支持 metadata filter、ACL、多租户、政策有效期。
- 增加 embedding cache、有限重试、批量本地 encode。
- 根据规模决定从 Milvus Lite 升级到 Milvus Standalone/Distributed。

## 13. 建议的功能开关与回滚点

```text
RAG_INDEX_VERSION=v1|v2
RAG_STRUCTURED_PARSING_ENABLED=false
RAG_TABLE_PARSING_ENABLED=false
RAG_OCR_ENABLED=false
RAG_OCR_ENGINE=paddleocr|docling|none
RAG_HYDE_ENABLED=false
RAG_HYDE_ROLLOUT_PERCENT=0
RAG_RERANKER_ENABLED=false
RAG_EVIDENCE_GATE_ENABLED=false
```

回滚策略：

- V1 与 V2 collection 并存，不覆盖唯一活索引。
- 所有重建先写 staging，校验行数、版本、随机抽检和黄金查询后再切换 alias/collection。
- manifest 记录当前 active index version 和上一版本。
- QueryPlanner/HyDE/reranker 均可单独关闭，关闭后退回原始 dense + lexical。
- 解析产物按 document hash + parser version 缓存，回滚代码不删除原始上传文件。
- OCR 失败不触碰当前线上索引。

## 14. 验收指标建议

以下是首轮目标，不是当前实测成绩：

### 14.1 解析与切块

- 100% 原始页面具有解析终态；静默丢页数为 0。
- 相同文档版本重复入库，重复 chunk 为 0。
- heading-only 叶子块为 0，除非标题本身就是独立可回答记录。
- chunk ID 全局唯一且跨相同版本重建稳定。
- 结构化表格中金额、单位、表头和合并关系可追溯到原单元格。

### 14.2 检索

- 黄金集 Recall@10 ≥ 95%。
- MRR@10 ≥ 0.80。
- 精确金额/日期/政策编号查询 top-3 命中率 ≥ 95%。
- no-knowledge precision ≥ 90%，防止无依据强答。
- 同一源的近重复块不超过最终证据的 40%。

### 14.3 HyDE

- 在预先定义的“语义困难集”上 Recall@10 至少提升 5 个百分点，或在相同 Recall 下减少 query variant。
- 精确查询 Recall/MRR 下降不超过 1 个百分点。
- HyDE 文本进入回答上下文或 source 的次数必须为 0。
- HyDE 故障时基础检索成功率不受影响。
- 单独报告新增 token 成本和 p95 延迟，超过预算自动降级。

### 14.4 回答与引用

- 数值/资格/审批/时限类 claim 的 evidence coverage = 100%。
- 引用文件、页码、section 与 block 定位准确率 ≥ 98%。
- 地区或国内/国际范围冲突导致的错误套用为 0。
- 低置信 OCR 证据若被引用，回答必须携带核对提示。

## 15. 必补测试清单

### 单元测试

- Markdown 多级 heading、列表、代码块、表格。
- 中文法规编号全覆盖。
- 标题与正文合并、token 边界和超长句。
- DOCX paragraph/table 交错顺序。
- XLSX 多 sheet、合并单元格、公式、日期、单位和空表头。
- PDF 空页、扫描页、混合页、加密、损坏、多栏、旋转、页眉页脚。
- 同 hash 增量幂等、文档版本替换、并发写入。
- QueryBundle 去重、HyDE dense-only、安全降级和缓存版本。
- rerank/evidence gate 的 score 序列化。

### 集成测试

- 上传 → 解析任务 → staging index → 原子发布 → 检索 → 引用回源。
- OCR worker 超时/崩溃后线上 V1 索引保持可用。
- embedding 429/5xx 重试与最终失败回滚。
- parser/chunker/embedding 版本变化自动要求重建。
- 表格行召回后回答引用对应 cell。

### 回归集必须包含

- “发票丢了怎么报销”与“遗失发票处理”。
- “出差期间生病的医疗费能报销吗”。
- 北京住宿标准与其他城市标准的范围隔离。
- 明确无答案的问题。
- 同一制度新旧版本冲突。
- 扫描表格中的金额、单位和脚注条件。

## 16. 本次审计的验证记录

本轮运行了现有 RAG 相关测试：

```text
pytest -q \
  tests/test_rag_pipeline.py \
  tests/test_rag_production_pipeline.py \
  tests/test_rag_agent.py \
  tests/test_knowledge_base_routes.py \
  tests/test_attachment_processing.py

结果：51 passed, 4 skipped
```

额外使用只读/临时目录诊断确认：

- 当前 10 个文件生成 365 块，257 块不足 100 字符。
- Markdown heading 不参与结构切块。
- 连续空格/tab 被普通规范化器压平。
- Pipeline 按 PDF 页切块时 `chunk_index` 每页回到 1。
- 同一文件重复增量入库产生相同 hash 的重复记录。
- 当前 manifest 不包含解析、切块、embedding 或 schema 版本。

现有测试全部通过只说明当前约定被实现，并不覆盖上述成熟度缺口。新增测试必须先复现这些问题，再开始改造。

## 17. 推荐实施顺序

最终建议顺序如下：

1. **评测和追踪先行**：没有 baseline 就无法判断 HyDE 是否真的改善。
2. **修复静默丢页、标题碎片、重复入库和 ID**：这是正确性问题。
3. **建立结构化 block 和格式感知 chunker**：这是表格/OCR/引用的共同基础。
4. **先接 DOCX、CSV、XLSX 原生结构**：成本低于 OCR，能快速获得表格能力。
5. **OCR/layout worker 小流量上线**：对扫描 PDF 和复杂表格按页回退。
6. **通用 reranker + evidence gate**：先控制弱证据进入回答。
7. **条件式 HyDE**：仅作为 dense 召回增强，A/B 后逐步放量。
8. **再迁移 BM25 和部署形态**：语料增长到 Python 全表扫描成为瓶颈时升级。

一句话原则：先让“真实文档被正确理解和切分”，再让“查询产生更多召回路径”。

## 18. 官方参考资料

- HyDE 原始论文：[Precise Zero-Shot Dense Retrieval without Relevance Labels](https://arxiv.org/abs/2212.10496)
- HyDE 官方代码：[texttron/hyde](https://github.com/texttron/hyde)
- Docling 支持格式：[Supported formats](https://docling-project.github.io/docling/usage/supported_formats/)
- Docling OCR/表格参数：[CLI reference](https://docling-project.github.io/docling/reference/cli/)
- Docling 表格结构序列化：[Serialization](https://docling-project.github.io/docling/concepts/serialization/)
- PaddleOCR PP-StructureV3：[Introduction](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-StructureV3/PP-StructureV3.html)
- PaddleOCR PP-StructureV3：[Usage tutorial](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
- OCRmyPDF 页面处理模式：[Advanced features](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- Milvus BM25：[BM25 Function](https://milvus.io/docs/bm25-function.md)
- Milvus dense/sparse hybrid：[Multi-Vector Hybrid Search](https://milvus.io/docs/multi-vector-search.md)
- BGE 中文模型卡与 query instruction：[BAAI/bge-large-zh](https://huggingface.co/BAAI/bge-large-zh)
