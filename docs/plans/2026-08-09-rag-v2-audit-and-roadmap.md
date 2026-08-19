# Hommey RAG V2 现状审计与改造路线图（修订版）

> 审计日期：2026-08-09（修订：2026-08-12）
>
> 审计范围：知识库上传、文档解析、OCR、规范化、切块、向量化、混合检索、查询改写、回答生成、引用与运维
>
> 本轮性质：只读审计与方案设计；未修改生产运行逻辑、索引或知识库原文
>
> 修订说明：按 2026-08-12 复核更新了与当前代码不一致的过时事实；把一期范围聚焦到"修复现有 RAG 系统的数据质量、幂等和可追溯问题"；HyDE 不进入一期验收，在 Phase Y 分水岭之后按 Phase 6a～6f 独立推进；补充了 Milvus 与 PostgreSQL 的存储决策分析；将远期元数据模型明确标为分期目标，不再作为一期一次性交付。

## 1. 结论先行

当前 RAG 已具备可运行的基础闭环：管理员上传 TXT/Markdown/PDF，后台全量重建，文本向量与 Python BM25 混合召回，经 RRF、ngram/focus 重排和少量领域过滤，再由 `ask-question` 生成受知识库约束的回答。全量重建已有蓝绿切换和失败保留旧索引机制，这是现阶段最可靠的一部分。

但它仍属于"纯文本、小规模、单领域的第一版 RAG"，尚未形成成熟的文档理解系统。最主要的问题不是缺少 HyDE，而是索引前的数据结构和切块质量不足：所有格式最终都被压成一个字符串并进入同一切块器，标题、表格、列表、页面版面和 OCR 信息没有得到保留。若直接叠加 HyDE，会提高召回调用量，却不会修复错误切块、扫描页丢失和证据污染，反而可能放大噪声。

综合判断：

- 未发现会立即破坏现有知识库的 S0 级灾难性缺陷；全量刷新失败时旧索引能保留。
- 确认存在 3 类直接正确性缺陷：扫描/混合 PDF 页静默丢失、标题碎片占用证据位、引用中的文件名可能被固定 `source` 值覆盖。另有增量幂等、弱证据拒答、版本追溯和 Skill schema 等 S2/S3 风险。
- 当前并非真正"只有 query 改写"：底层还有向量 + BM25 + RRF；但所谓改写主要是上游 `rewritten_query`、去口语化和少量硬编码扩展（`expand_query` 的国际/餐费同义词），没有统一的查询规划、HyDE、通用重排或完整检索追踪。
- PDF OCR 目前不是"部署不合理"，而是根本未部署。现有 `pypdf` 只能提取已有文本层，不能识别扫描图像。
- 表格当前既没有知识库上传支持，也没有结构化索引。聊天附件虽能读 DOCX 表格，但会破坏正文与表格的原始顺序，而且不会进入长期知识库。聊天附件现已支持图片（VisionClient 走 Qwen2.5-VL），但图片内容同样不进知识库。
- 存储层（Milvus Lite）在当前规模下并不重、与 PostgreSQL 无冲突；本期不做数据库迁移，详见 §8 的决策分析。
- 推荐先完成结构化解析、格式感知切块、去重和评测基线；HyDE 本期不做，留待基础数据质量修复完成后再评估。

**本期的三条承诺边界**：

1. **只修现有 RAG 系统的数据质量和可追溯问题**：页面静默丢失、标题碎片化、增量幂等、稳定 chunk 身份、引用文件归因和最小版本信息。改造方式是泛化设计（§7 的"切块器只消费结构"），但一期不同时上马 ACL、多租户、bbox、表格单元格定位和历史索引归档。
2. **存储保持 Milvus Lite，不做迁移**：理由见 §8。迁移决策推迟到出现明确触发条件。
3. **HyDE 本期不做**：完整设计保留在附录 A，作为未来召回增强的参考，不进入本期验收。

## 2. 当前链路全景

```text
管理员知识库                         聊天附件（另一条链路）
TXT / MD / PDF                      TXT / MD / DOCX / PDF / 图片
      │                                   │
      ▼                                   ▼
FileSystemDocumentLoader             AttachmentService.upload
      │                                   │
      ▼                                   ▼
TxtParser / PdfTextParser(pypdf)     ProcessorRegistry
      │                                   ├─ Txt/Docx/PdfProcessor
      ▼                                   └─ ImageProcessor(VisionClient)
ParsedDocument(text)                       │
      │                                   ▼
      ▼                            extraction 存库，最多 12000 字符注入请求
TextNormalizer                           │  不进入知识库索引
      │                                   ▼
      ▼                            normalize → agent_query / display_message
TextChunker(600 字符/100 重叠)
      │
      ▼
Embedding API（siliconflow bge-m3）
      │
      ▼
Milvus Lite dense vector + Python 全量扫描 BM25
      │
      ▼
RRF 融合 + ngram/focus 重排 + 餐费弱过滤
      │
      ▼
top_k=3 → ask-question → LLM 总结 → sources
```

关键代码入口：

- 配置：`rag/config.py`（`RAGPipelineConfig.from_settings()`）
- 加载：`rag/loader.py`（`FileSystemDocumentLoader`）
- 解析：`rag/parser.py`（`TxtParser` / `PdfTextParser`）
- 规范化：`rag/normalizer.py`
- 切块：`rag/chunker.py`（`TextChunker`）
- 入库编排：`rag/pipeline.py`
- 存储和混合检索：`rag/milvus_store.py` / `rag/vector_store.py`（`VectorStore` ABC + `InMemoryVectorStore`）
- 检索入口：`rag/retriever.py`（`VectorStoreRetriever` / `KnowledgeRetriever` / `expand_query`）
- Embedding：`rag/embedder.py`（`SiliconFlowEmbedder`）
- RAG Agent：`.agents/skills/ask-question/script/agent.py`
- 知识库管理：`webui_new/knowledge_base_service.py`
- 聊天附件解析：`multimodal/document_processor.py` / `multimodal/image_processor.py` / `multimodal/vision_client.py`

## 3. 成熟度评估

| 能力 | 当前等级 | 结论 |
|---|---:|---|
| 上传与文件安全 | 2.5/5 | 有后缀、大小、空文件和同名保护，但文件类型少，PDF 只校验 `%PDF` 文件头 |
| 文档解析 | 1.5/5 | TXT/MD/PDF 文本层可用，没有统一文档元素模型、版面、表格或 OCR |
| 切块 | 1/5 | 单一字符切块器；当前语料严重碎片化，不按格式和 token 处理 |
| 索引一致性 | 2.5/5 | 全量蓝绿刷新可靠；增量写入没有去重，版本元数据不足 |
| 检索 | 2.5/5 | 已有 dense + BM25 + RRF + ngram/focus 重排；BM25 不可扩展，过滤仅覆盖餐费领域，缺少通用重排和门槛 |
| Query 理解 | 2/5 | 有去口语化和 `expand_query` 局部同义扩展，但无统一 QueryBundle、HyDE、实体/范围约束 |
| 回答约束 | 3/5 | 提示词对证据约束和提示注入防护较好；缺少 claim-citation 校验 |
| 引用与可观测性 | 1/5 | 文件归因可能被固定 `source` 值覆盖，section 基本不真实；缺少 excerpt 和完整评分链路 |
| 评测与回归 | 1.5/5 | 单元测试可用（62 passed / 0 skipped），但无黄金查询集、Recall/MRR/nDCG、OCR/表格质量评测 |
| 部署与扩展 | 2/5 | Milvus Lite 适合当前小语料；现有入库解析在 Web 进程后台线程中，未来不应直接承载 OCR/layout 重任务 |

### 3.1 本次进程解析到的实际配置

| 配置 | 当前值 |
|---|---|
| Embedding backend | `siliconflow` |
| Embedding model | `BAAI/bge-m3`（`.env` 与 dataclass 默认值一致） |
| Embedding dimension | `1024` |
| Chunk size / overlap | `600 / 100` 字符 |
| Final / vector / BM25 top-k | `3 / 10 / 10` |
| Embedding batch size | `32` |
| Knowledge file types | `txt, md, pdf` |

> 过时事实修正：上一版记录"dataclass 默认 bge-m3、环境解析为 bge-large-zh-v1.5"已不成立——当前 `.env` 第 22 行就是 `BAAI/bge-m3`。但 **manifest 仍不保存最终生效值**，仅查看索引无法知道它由哪个模型生成的问题依然存在（见 §4.8）。

### 3.2 真实知识库检索抽样

以下为本轮对现有 Milvus 知识库的只读查询观察，不代表完整评测，也不足以单独支撑门槛或 reranker 的选型结论：

| 查询 | 结果概况 | 暴露的问题 |
|---|---|---|
| 北京出差住宿费标准是多少 | 第 1 名正确命中北京 500 元条款，但 top-3 混入其他城市 300/400 元条款 | 城市范围缺少 metadata filter，回答模型承担二次辨别风险 |
| 我出差有餐补吗 | FAQ 与餐费条款靠前 | 餐费硬编码过滤有效，但不可泛化 |
| 出差期间生病的医疗费能报销吗 | 真正相关 FAQ 位于第 3 名，第 1 名是独立文档标题 | 标题碎片污染排序 |
| 发票丢了怎么报销 | top-3 未命中"遗失发票"条款 | 口语同义召回失败，且无 evidence gate |
| 高铁可以坐什么座位 | 前两名正确，第 3 名混入飞机公务舱 FAQ | top-k 中存在跨费用类型噪声 |
| 2025年公司的差旅碳排放目标 | 第 1 名正确，第 2 名是独立章节标题 | 标题块重复占用证据槽位 |

这组抽样说明当前系统不是完全不可用：精确术语和餐费领域已有较好结果；主要风险集中在同义表达、范围约束、标题碎片和弱相关结果拒绝。

## 4. 已确认的 Bug 与风险

> 严重级别：S1=直接影响回答正确性/数据完整性的缺陷；S2=潜伏或规模放大后影响；S3=健壮性/一致性小问题。级别仅代表优先级，不代表可行性。

### 4.1 S1：扫描 PDF 和混合 PDF 页面会静默丢失

现状：`PdfTextParser` 对每页调用 `pypdf.page.extract_text()`。返回空字符串的页面被直接 `continue`，不会生成 `parse_error`，不会标记需要 OCR，也不会记录"本页未索引"。

影响：

- 纯扫描 PDF 会生成 0 个页面；全量刷新在没有其他有效文件时失败，但无法告诉管理员真实原因是需要 OCR。
- 混合 PDF 更危险：有文字层的页面正常入库，扫描页悄悄消失，刷新仍可能显示成功。
- manifest 会把未报错的源文件标为 `indexed`，造成"界面显示已索引，但部分页不可检索"的假成功。
- 聊天附件的 PDF 解析同样会把空页和异常页静默跳过。

泛化修改方向：建立"每个原始页面必须有一个终态"的契约（`native_text` / `ocr_text` / `intentionally_skipped` / `error`），任何页面都不能无记录消失。这条规则对 PDF 页、OCR 页、解析失败页是同一类约束，不单独针对 PDF 打补丁（详见 §7 原则 P8）。

### 4.2 S1：当前切块严重过碎，标题成为无意义独立证据（孤儿切块）

当前配置是 600 字符、100 字符重叠，但真实语料并未接近这个上限。对现有 `data/documents` 进行不写库重算（2026-08-12 复核，14 个文件，含 4 个 PDF）：

| 指标 | 实测值 |
|---|---:|
| 源文件 | 14 |
| 生成块 | 384 |
| 最短 | 4 字符 |
| P25 | 49 字符 |
| 中位数 | 74.5 字符 |
| P75 | 131 字符 |
| 最长 | 600 字符 |
| 小于 100 字符 | 248 / 384（64.6%） |
| 小于 20 字符（孤儿候选） | 66 |

实测中可确认的典型孤儿标题包括："一、差旅申请流程""二、交通标准""三、住宿标准""四、餐饮标准""五、其他费用标准""六、差旅补贴""七、特殊情况处理""八、违规处理"。但“小于 20 字符”只是候选口径，不能把 66 个全部等同为标题；验收时应按 `heading-only` 结构规则重新统计。碎片的直接原因是切块器遇到章节标题立即冲刷当前块，下一条编号子标题再次冲刷。实际检索中，"出差期间生病的医疗费能报销吗"的第 1 名就出现了仅含"差旅费用报销规定"的标题块，而真正 FAQ 位于第 3 名。

根因不是"这组正则不够全"，而是**切块器的输入是裸文本，结构在进入切块器前就丢了**。正则再多也无法可靠地区分"标题"和"正文首行"。泛化修改方向见 §7：切块器只消费结构（block 序列），标题作为 `heading_path` 附加到其后正文，短小相邻条款合并，超长条款才在句子/token 边界拆分。这会一并解决 §4.5、§4.7、§3.2 中"标题碎片污染排序"的所有同类现象。

### 4.3 S2：增量入库没有去重，会重复写入相同 chunk

`add_chunks()` 直接按当前行数追加，未按 `hash`、`document_id + logical_chunk_id` 或文档版本 upsert。实测同一 TXT 文件连续两次 `rebuild=False`：第一次新增 1 块，第二次仍新增 1 块；两行 hash 完全相同。

影响：

- CLI 或未来增量上传重复执行会制造重复召回。
- 重复块占据 top-k，使证据多样性下降。
- 并发增量写入都基于 `current_count` 计算主键，存在 ID 冲突窗口。

当前 Web 管理页使用全量 `rebuild=True`，所以日常刷新暂时绕开了该问题；它是已确认的公共接口幂等缺陷，但尚无证据表明当前 Web 生产路径正在因此产生重复数据，因此定为 S2。

泛化修改方向：以"稳定 chunk hash + document_id + document_version"为幂等键做 upsert；文档更新采用"新版本写入 → 校验 → 按 document_id 切换/删除旧版本"，而不是 append。这是一条针对"所有可写路径"的幂等约束，不只修 CLI。

### 4.4 S2：缺少通用相关性门槛，弱相关结果仍会成为候选证据

`hybrid_search()` 始终从向量和 BM25 取排名结果，RRF 后直接返回 top-k；没有经过语料标定的相似度阈值、通用 reranker 阈值或 no-knowledge 判定器。`filter_relevant_results()` 只在餐费领域产生 term，且仍无条件保留 vector rank 1 和 BM25 rank 1。

实测查询"发票丢了怎么报销"未命中语料中的"遗失发票"条款，前三名分别偏向发票验真、无关标题、超标准金额。这能确认候选召回与拒答机制有缺口，但单条查询尚不足以证明已导致 S1 级最终答案错误；严重度应由黄金查询集上的答案错误率和 no-knowledge 误判率复核。

泛化修改方向：建立通用重排器和 `evidence_gate`，校准"可回答 / 部分依据 / 无依据"；原始排名第一不再等于强制有效证据。门槛应对所有查询域生效，而非只对餐费（详见 §9.4）。

### 4.5 S2：并未按 Markdown、表格或 PDF 结构切块

`TxtParser` 同时处理 TXT 和 MD，Markdown 的 `#` / `##` 标题不会触发当前主题规则。诊断样例中的两级 Markdown 标题和两段正文被合成同一个块。当前主题规则只识别：

- `Q数字:`（`_QUESTION_HEADING_RE`）
- `一、标题`（`_SECTION_HEADING_RE`，仅 `[一二三四五六七八九十]`）
- `1. 标题`，且点号后必须有空白（`_NUMBERED_HEADING_RE`）

它不识别 Markdown heading、`1、`、`（一）`、`第X章`、列表、代码块、表格、引用块或 PDF 版面元素。根因与 §4.2 相同：输入是裸文本，结构未保留。泛化解法见 §7 原则 P1/P4：解析器产出带 type 的 block 序列，切块器按 type 分派，主题识别是"可注册的规则注册表"而非三条硬编码正则。

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

这对普通段落无害，但会丢失定宽表格、代码块、缩进列表和 OCR 版面信息。泛化修改方向：**按 block type 规范化**（段落压空白、代码/表格/OCR 版面保留），而不是"全局压一遍"（详见 §7 原则 P5）。

### 4.7 S2：PDF 的 `chunk_index` 和 `chunk_id` 不稳定

Pipeline 对每个规范化页面分别调用 `chunker.chunk([document])`，因此 `chunk_index` 在每页重新从 1 开始。Milvus metadata 又将 `chunk_id` 写为 `{文件名 stem}_{chunk_index}`，多页 PDF 会产生重复 `chunk_id`（本次复核：4 个 PDF 共产生 5 组重复 `chunk_id`）。hash 因包含页码仍不同，但对日志、引用和未来 upsert 不可靠。

泛化修改方向：采用从文档谱系派生的稳定身份，例如：

```text
document_id / document_version / page_number / block_id / chunk_ordinal
```

并以此作为幂等 upsert 键（详见 §7 原则 P6）。

### 4.8 S2：解析与索引版本不可追溯

当前 `ingestion_manifest.json` 只有：

- `refreshed_at`
- 文档 `sha256` 和 `indexed_at`
- 汇总 report

没有 `schema_version`、parser/chunker 版本、OCR 模型、embedding 模型与维度、query instruction、索引配置或代码版本。改变 embedding、切块或解析算法后，系统无法判断是否必须重建，也无法复现某条回答使用的索引版本。

### 4.9 S2：当前 BM25 每次查询扫描全部文档

`bm25_search()` 每次查询都：

1. 按整数 ID 范围从 Milvus 拉取全部块；
2. 重新 tokenize 全部文档（`_tokenize` 现已生成 2/3/4-gram 中文 ngram，词项数约为逐字的 4 倍，加剧耗时）；
3. 重新统计 DF 和 BM25；
4. 在 Python 中排序。

384 块时尚可，扩展 DOCX/XLSX/PDF/OCR 后会随语料线性恶化。`fetch_all_documents()` 还假定 ID 从 1 到 count 连续，未来删除或 upsert 后会漏数据。

Milvus 官方已支持 BM25 sparse 和 dense/sparse hybrid，但官方集成说明 Milvus Lite 的内建 full-text 能力存在部署版本限制。因此应先核实当前 `pymilvus/milvus-lite` 组合；若 Lite 不支持，选择预计算 BM25 索引。**不建议为了 BM25 而升级 Milvus Standalone**——这是算法问题，不是存储问题（详见 §8.3）。

### 4.10 S2：重排已部分泛化，但过滤仍只覆盖餐费且保留"rank 1 免检"

上一版记录"重排规则几乎只覆盖餐费领域"已不准确：`_query_ngrams`（3/4-gram）与 `_focus_terms`（去除"出差期间/是否可以报销"等套话后取具体主语）已加入评分，`_rerank_terms` 也已包含"报销"。但剩余问题仍然成立：

- `filter_relevant_results()` 仍在 `_rerank_terms()` 非空（即查询含餐费词）时才启用过滤，否则直接放行全部结果；且保留 `vector_rank == 1` 或 `bm25_rank == 1` 无条件通过。
- `_off_topic_penalty()` 仍是餐费专用（`餐补/餐费/餐饮/饭补/吃饭` 触发）。
- 变量名虽叫 rerank，实际是 ngram/focus 的启发式加权，不是 cross-encoder 或通用语义重排器。
- 内部产生的 `rerank_score` 没有进入 `RetrievalResult.to_dict()`，日志和调用方看不到最终排序依据（`InMemoryVectorStore._result_from_dict` 同样丢弃该字段）。

泛化修改方向：把"过滤/门槛"与"重排"拆开——重排可先保持轻量 ngram/focus，但证据门槛必须是通用机制（§9.4），不因查询含不含餐费词而缺席。

### 4.11 S2：Embedding 配置和查询方式缺少契约

当前 manifest 不保存实际 embedding 模型和维度；本地 SentenceTransformer 路径逐条 encode，未批量处理。若使用 BGE 中文 v1.5，官方模型卡对短查询检索推荐 query instruction，语料 passage 不加 instruction；当前 `embed_query()` 与 `embed_texts()` 完全同路（`embed_query` 只是 `embed_texts` 的 1 元素封装），未形成可配置的 query/passage 编码契约。当前模型为 bge-m3，其 instruction 建议需以离线评测决定是否启用。

这不是断言"当前结果必然错误"，但应通过离线评测决定是否启用 instruction，并把选择写入索引版本。

### 4.12 S1：引用文件归因可能错误，且无法稳定指向真实章节

`FileSystemDocumentLoader` 当前把 metadata `source` 固定写为 `business_travel_documents`，而 `ask-question._serialize_source()` 的 `file` 又优先取 `source`，然后才取 `file_name/filename`。因此当前 `sources[].file` 可能输出一个类别常量，而不是实际文件名。这是正在运行路径上的引用正确性 Bug，不只是未来高亮能力不足。

另外，当前 chunk 没有真实 section，`title` 通常是文档或 PDF 页的第一行；输出 schema 对 source item 也没有严格字段约束。一期必须先修正 `file=filename`、传递真实 page 并提供可复核 excerpt；内部追溯使用 `document_id/source_path`，不把服务器绝对路径暴露为用户展示的 `file`。`heading_path/block_id` 随 block 模型落地。bbox 和 table cell 定位属后续格式能力，不作为一期必须字段。

### 4.13 S2：RAG Agent 的异常输出与 Skill schema 不一致（潜在）

> 严重级从上一版 S1 下调为 S2：经复核，当前编排链路并没有在运行时对 Agent 输出执行 JSON Schema 校验（`_normalize_agent_payload` 把任何非 error 状态归一为 success），因此这不是实时故障，而是"一旦启用 schema 校验就会爆"的潜伏契约缺陷。

`.agents/skills/ask-question/schemas/output.json` 只允许 `status=success|no_knowledge`，并要求 `status` 和 `answer` 必填；但 Agent 实际还会返回：

- 初始化失败时：`status=error`，只有 `message`，没有必填的 `answer`；
- 知识库为空时：`status=knowledge_base_empty`。

这属于明确的接口契约 Bug。只要编排器对 Skill 输出执行 schema 校验，最需要稳定处理的异常路径反而可能被判为无效输出，进而触发二次 fallback 或掩盖原始错误。

泛化修改方向：统一状态机和 schema，至少定义 `success | partial | no_knowledge | knowledge_base_empty | error`；使用 JSON Schema 条件约束各状态必填字段，并为每个异常分支加入端到端验证。这与 §10 的回答框架是同一个状态机，不应存在两套。

### 4.14 S3：文本编码策略不一致

知识库 TXT/MD 强制 UTF-8，聊天附件则依次尝试 UTF-8 BOM、UTF-8、GB18030、GBK。相同文件在聊天中可读，在知识库上传时可能被拒绝。应统一策略，并在管理界面明确展示检测结果；不建议用 `errors=ignore` 静默吞字符。

### 4.15 S3：Embedding API 没有重试与退避

`SiliconFlowEmbedder._request_embeddings` 当前只有超时、响应数量和维度校验，没有针对 429/5xx/瞬态网络错误的有限重试、指数退避和批次级恢复。全量蓝绿机制能保旧索引，但一次瞬态错误会让整次刷新失败并重新计算全部向量。

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
| 图片 | 不支持 | 不支持 | 支持（VisionClient Qwen2.5-VL） | 否 | 聊天可读图，但结果不进知识库 |
| PPTX | 不支持 | 不支持 | 不支持 | 否 | 可放后续阶段 |

> 过时事实修正：上一版"图片三列全不支持"已不准确——聊天附件现支持图片，走 `multimodal/image_processor.py` → `vision_client.py`（Qwen2.5-VL，含重试与退避、每日配额）。但图片识别结果仅注入当次对话上下文，不进入知识库索引。这正好构成一个可复用的 OCR 前端（详见 §9.1）。

## 6. 目标数据模型：先保留结构，再决定如何切块

不应继续让所有解析器只返回一个 `text`。建议建立中间文档模型——这也是 §7 泛化切块的前提：

> **范围说明**：下面是目标上限，不是 Phase 1 必须一次实现的 47 个字段。一期最小集为 `document_id/document_version`、`page_number/page_terminal_state`、`block_id/block_type/heading_path/text`、`chunk_id/chunk_hash/chunk_ordinal`、`retrieval_text/display_text` 和 parser/chunker/embedding 版本。ACL、tenant、bbox、表格单元格谱系、索引历史归档都由后续真实需求触发。

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

**为什么这同时是泛化关键**：一旦解析器统一产出最小 block 结构，切块器、规范化器、引用字段和幂等键就有了稳定输入。之后新增 DOCX/XLSX/OCR 解析器时，应尽量复用同一切块契约；但表格、版面和跨页规则仍可能需要扩展分派逻辑，不承诺“切块与检索层零改动”。


### 6.1 元数据模型设计（目标上限，分期实现）


本节描述长期分层模型，供新格式、权限过滤和强审计需求出现时参考。Phase 1 只实现本节中服务于页面终态、结构切块、稳定身份、正确引用和版本追溯的最小字段集。其余字段不得仅因为出现在目录中就进入当期开发。

#### 6.1.1 设计原则

一、五层分层与单一写主。RAG 元数据按 document / page / block / chunk / operational 五层建模。每个字段有且仅有一个权威写入者（单一 gate）：loader 只写 document 层身份字段，parser 只写 page 层与 block 层并自报 parser_name/parser_version，chunker 只写 chunk 层，pipeline 在入库前盖章 operational 层，indexer 是唯一落库写点并写 ingested_at/collection_name，admin 只写 manifest 与 job_state。装配单向推进 loader → parser → chunker → pipeline → indexer → admin，前层产物是后层输入，任何层不得回头改写前层字段。写主语义补充一条 provenance 规则：admin 权威字段（category/department/jurisdiction/effective_date/expiry_date/policy_status/acl）允许 parser 抽取作为回填源，但回填必须记来源（provenance），且绝不静默覆盖 admin 显式写入值；admin 覆盖走显式路径。

二、唯一装配点不变式。DocumentChunk.to_metadata() 仍是 metadata dict 的唯一出口，职责从拍平 dataclass 字段扩展为分层投影加 pydantic schema 校验：先取 pipeline 盖章的 operational 层，再取继承 metadata（仅 document/page 层引用字段白名单），最后拍平 chunk 身份字段。输出前过 schema 校验，非法字段名或旧别名直接抛错。任何解析器、切块器不得绕过它直接写键。

三、字段服务于四类用途。所有字段必须至少服务身份、检索质量、引用、审计四类用途之一（允许兼项），无用途字段不落库。检索所需字段冗余进 metadata，纯审计明细（block 明细、页终态明细）只在 manifest 侧保留，回答载荷按白名单投影。

四、格式无关与泛化。字段名应尽量格式无关：TXT/MD/PDF/DOCX/XLSX/OCR 由不同解析器产出共同的最小 block 契约；表格、版面和跨页逻辑可以通过新 block type 和可测试的分派扩展，不承诺新格式只改 parser。身份构造不以 PDF 页为唯一锚点：location 段泛化（PDF=page、XLSX=sheet/row、TXT/MD=章节序号），sources 输出的 page 字段可空，无分页文档使用独立 location 字段而不过载 page 语义。

五、过滤不替代召回。范围/时效/权限过滤只负责把不适用、过期、无权见的证据挡在回答之外，绝不替代召回质量。词表外部化：所有受控词表随元数据 schema 版本化为 data/config 数据文件（JSON/YAML），代码只读表执行，不残留点状硬编码。

#### 6.1.2 分层字段目录

#### document 层（16 字段）

| 字段 | 类型 | 单一写入者 | 用途 | 是否可过滤 | 示例 |
|---|---|---|---|---|---|
| document_id | str | loader 计算，pipeline 汇总校验 | 身份（谱系根）：相对 documents_dir 的 posix 路径，与 ingestion_manifest documents 键对齐 | 是 | policy/2026差旅费用规定.pdf |
| document_version | str（sha256 前 12 位） | loader | 身份/审计：文件内容版本，chunk_id 与幂等键组成部分 | 是 | a1b2c3d4e5f6 |
| sha256 | str（64 位 hex） | loader | 审计：完整性校验与 manifest 溯源 | 否 | 9f86d081884c7d65… |
| source_path | str | loader | 运行期回源路径，弃用 file_path 别名；绝对路径不作为跨环境身份 | 否 | /home/hlq/Hommey/data/documents/policy/2026差旅费用规定.pdf |
| filename | str | loader | 引用：展示名，弃用 parent_doc/file_name/name 别名 | 否 | 2026差旅费用规定.pdf |
| file_type | str | loader | 审计/过滤：格式类型 | 是 | pdf |
| mime_type | str | loader | 审计：真实 MIME | 否 | application/pdf |
| title | str | parser | 引用/检索：文档级标题（取文档首 H1 或文件名 stem），不再取页首行 | 否 | 2026年度差旅费用管理规定 |
| category | str | admin（权威写主）；parser 回填默认仅记 provenance | 检索/过滤：文档分类，受控词表，不再硬编码 business_travel | 是 | business_travel |
| department | str \| null | admin；parser 可从文档头部回填 | 过滤：部门归属 | 是 | finance |
| jurisdiction | object {scope, countries, cities, region} | admin（权威写主）；parser 抽取回填 | 过滤/检索：适用范围，根治北京条款混入其他城市 | 是 | {scope: domestic, cities: [北京]} |
| effective_date | date \| null | admin；parser 文档头回填 | 过滤/审计：时效起点 | 是 | 2026-01-01 |
| expiry_date | date \| null | admin | 过滤：时效终点 | 是 | 2026-12-31 |
| policy_status | enum current/superseded/draft/expired | admin（manifest/文档版本切换标记） | 过滤/审计：现行性判定 | 是 | current |
| acl | object {tenant_id, visibility} | admin | 过滤：权限边界（visibility=public/internal/restricted，tenant_id 多租户预留） | 是 | {visibility: internal, tenant_id: t-001} |

#### page 层（8 字段）

| 字段 | 类型 | 单一写入者 | 用途 | 是否可过滤 | 示例 |
|---|---|---|---|---|---|
| page_number | int | parser | 身份/引用：页码，弃用 page 别名 | 是 | 3 |
| page_terminal_state | enum native_text/ocr_text/intentionally_skipped/error | parser | 审计：P8 页终态，任何页不得无记录消失 | 是 | native_text |
| parse_mode | enum native/ocr/mixed | parser | 审计/检索质量：页面解析方式 | 是 | mixed |
| ocr_confidence | float \| null | parser | 检索质量：证据门槛信号，低置信可降权或剔除 | 是 | 0.92 |
| width | int | parser | 审计/引用：bbox 坐标系基准 | 否 | 595 |
| height | int | parser | 审计/引用：bbox 坐标系基准 | 否 | 842 |
| page_hash | str | parser | 审计/幂等：页面规范化文本哈希，参与 block_id 漂移检测 | 否 | c4e5f6a7b8 |
| warnings | list[str] | parser | 审计：页面级告警（低置信 OCR、空页已转 OCR 等） | 否 | [低置信 OCR, 空页已 OCR] |

#### block 层（8 字段）

| 字段 | 类型 | 单一写入者 | 用途 | 是否可过滤 | 示例 |
|---|---|---|---|---|---|
| block_id | str | parser | 身份/谱系：p{page}-b{seq}，由 reading_order 确定性派生；只承诺在相同文档版本和 parser 版本下稳定 | 否 | p3-b5 |
| block_type | enum heading/paragraph/list/table/code/image/faq/footer/other | parser | 检索质量：切块分派依据，表格/代码保空白 | 是 | table |
| heading_path | list[str] | parser（heading stack 累积） | 检索/引用：P2 章节路径，替代 section | 否 | [一、交通标准, （一）高铁] |
| block_level | int | parser | 检索质量：标题层级 | 否 | 2 |
| bbox | list[4] float | parser | 引用：版面定位，支持页面高亮 | 否 | [72, 100, 520, 140] |
| reading_order | int | parser | 审计/谱系：block 序号稳定性来源 | 否 | 7 |
| table_data | object {sheet, table_id, cells, row_span, col_span} | parser | 引用：表格结构，供表格定位与裁剪 | 否 | {sheet: 报销标准, table_id: t1} |
| block_hash | str | parser | 审计/幂等：block 内容指纹，校验解析稳定性 | 否 | d1e2f3a4 |

#### chunk 层（16 字段）

| 字段 | 类型 | 单一写入者 | 用途 | 是否可过滤 | 示例 |
|---|---|---|---|---|---|
| chunk_id | str | chunker | 身份：谱系派生、全局唯一，在相同文档/parser/chunker 版本下跨重建稳定（格式见 6.1.3），indexer 不再追加改写 | 否 | policy/2026差旅费用规定.pdf::a1b2c3d4e5f6::p3::p3-b5::02 |
| chunk_hash | str | chunker | 身份/幂等：幂等键组成部分（chunk_hash+document_id+document_version） | 否 | e5f6a7b8 |
| chunk_ordinal | int | chunker | 身份：文档级序号，不再每页重置 | 否 | 12 |
| chunk_index_within_page | int | chunker | 身份：页内序号，与 chunk_ordinal 分离 | 否 | 2 |
| content_type | enum 同 block_type | chunker | 检索质量：重排加权依据（heading 不加权、faq 加权） | 是 | paragraph |
| page_start | int | chunker | 引用/过滤：跨页块起点页 | 是 | 3 |
| page_end | int | chunker | 引用/过滤：跨页块终点页 | 是 | 4 |
| block_ids | list[str] | chunker | 谱系/审计：该 chunk 归并覆盖的 block 列表，血缘回源入口 | 否 | [p3-b5, p3-b6] |
| heading_path | list[str] | chunker（继承自 block） | 检索/引用：供精确加权与引用投影 | 否 | [一、交通标准] |
| retrieval_text | str | chunker | 检索质量：带标题路径/表头/同义标签的检索文本，BM25 打分对象 | 否 | 【一、交通标准】（一）高铁：一等座凭据实报销 |
| display_text | str | chunker | 引用：保持可引用的原始内容，注入 LLM 上下文 | 否 | （一）高铁：一等座凭据实报销 |
| text_source | enum native/ocr/mixed | chunker（继承自 page.parse_mode） | 审计/证据质量：文本来源（改名避免覆盖旧 source 引用键） | 是 | ocr |
| table | object \| null {sheet, table_id, row, col, row_span, col_span} | chunker（收敛自 block.table_data） | 引用：表格级定位，回答可精确到单元格 | 是 | {table_id: t1, row: 2} |
| source_offsets_bbox | list[list[4]] | chunker（由 block bbox 合并） | 引用：页级版面区间，支持高亮 | 否 | [[72, 100, 520, 140]] |
| ingested_at | datetime | indexer | 审计：入库时间戳 | 是 | 2026-08-12T09:30:00+08:00 |
| index_version | str | pipeline 注入，indexer 落库 | 审计/可复现：块所属索引指纹 | 是 | 3f2a9c1d0b4e88aa |

#### operational 层（8 字段）

| 字段 | 类型 | 单一写入者 | 用途 | 是否可过滤 | 示例 |
|---|---|---|---|---|---|
| schema_version | str | pipeline | 审计：序列化契约版本（统一命名空间 rag.v2.metadata.1），写入时盖章并记入 manifest | 是 | rag.v2.metadata.1 |
| parser_name | str | parser | 审计：产出本数据的解析器名 | 否 | pdf_text |
| parser_version | str | parser | 审计：触发重建信号之一 | 是 | pdf-p0-1 |
| chunker_version | str | chunker | 审计：触发重建信号之一 | 是 | chunker-v2-0 |
| embedding_model | str | pipeline | 审计：实际生效的 embedding 模型 | 是 | BAAI/bge-m3 |
| embedding_dimension | int | pipeline | 审计：实际生效的向量维度 | 否 | 1024 |
| collection_name | str | indexer | 审计：落库集合（蓝绿切换可追溯） | 否 | business_travel_knowledge |
| job_state | object {job_id, status, stage, progress, started_at, finished_at} | admin | 审计：刷新任务运行态（内存驻留，不落盘） | 否 | {status: running, progress: 0.4} |

注意：检索期分数字段 rerank_score / fusion_score / vector_rank / bm25_rank / retrieval_trace_id 不属于持久化目录，不落 Milvus metadata，只存在于 RetrievalResult 与 trace（见 6.1.5、6.1.6）。范围/权限派生字段 domestic_international（domestic|international|general，由 jurisdiction.scope 展平）与 city（string[]，由 jurisdiction.cities 展平）为 derived 层，并入 document 层继承语义，单一写入者为唯一派生函数（见 6.1.4）。

#### 6.1.3 身份与谱系

一、chunk_id 谱系派生。chunk_id 由文档谱系派生，格式为：

    {document_id}::{document_version}::{location}::{first_block_id}::{ordinal}

其中 location 段泛化：PDF 为 page（p3），XLSX 为 sheet/row（s2-r4），TXT/MD 为章节序号（c2）；first_block_id 为该 chunk 覆盖的首个 block；ordinal 为文档内补零序号。示例：policy/2026差旅费用规定.pdf::a1b2c3d4e5f6::p3::p3-b5::02。该格式是全文唯一规范，trace.results 示例与回答输出一律采用，不再出现三处三格式的分叉。

二、block 模型为硬前置。chunk_id 依赖 reading_order 派生的 block 序号，当前代码不存在 block 模型（parser 只产页面级文本），因此跨重建稳定在 block 模型建成前无法成立。落地时先建立 block 模型（block_id/reading_order/heading_path/block_type），未建成前退化为 document_version+page_number+chunk_ordinal 的稳定组合，并在 manifest 明确标记跨重建稳定暂不成立。漂移检测：以 block_hash/page_hash 比对两次解析是否稳定，漂移时回退按 chunk_hash+document_id+document_version 幂等去重，保证增量入库不被重复。

三、单一写主裁决。chunk_id 唯一写主为 chunker（P6 谱系派生），indexer 的 milvus_store.add_chunks 与 replace_chunks_atomically 停止追加改写 chunk_id（当前 {stem}_{chunk_index} 格式废除），只做 idempotent upsert（先按幂等键查重，后按 document_id 蓝绿切换版本）并写 ingested_at/collection_name。行主键 id 与 chunk_id 严格分离。

四、谱系血缘。chunk 通过 chunk_id 内嵌 document_id/version/location 与 block_ids 双向追踪：正向从文件到 chunk（loader→parser→chunker 逐层 id），反向从 chunk 到原文件（block_ids → block_hash → page_terminal_state → source_path）。回答引用只投影 display_text/excerpt/page_start/heading_path/bbox/table，形成可回源、可审计的引用链。

#### 6.1.4 范围与权限过滤

一、注册表唯一事实源。FilterableFieldRegistry 随元数据 schema 一起版本化（初始 v1），每行声明：name（与 metadata 键一致）、level（document 继承 / chunk 级 / derived 派生 / query 查询侧）、type 与取值域（受控词表优先，null 表示通用或未标定）、authoritative_writer（权威写主，单一 gate）、backfill_sources（回填源列表，记 provenance）、query_extractor_rule（查询抽取规则）、revalidate_rule（检索后重校验谓词）、tier（filter 查询期过滤 / permission 权限过滤 / display 仅显示引用）、index_required（是否需索引存储，即 6.1.5 与 §8.3 触发集合）、storage_hint（Milvus 标量表达式 / PG 列 / 仅 metadata 透传）。词表（category/city/国家/国际关键词/费用词/职级词）强制外部化为 data/config 数据文件，随元数据 schema 版本化，代码只读表执行；DEFAULT_CATEGORY_MAPPING、_DOMAIN_TERMS、expand_query 的国际/餐费硬编码、_rerank_terms/_off_topic_penalty、agent 多查询扩展词全部迁入注册表数据行并删除代码内硬编码。category 权威写主为 admin，business_travel 保留为 v1 兼容值，未标定文档不回跳类别。

二、注册表核心字段。domestic_international（derived，domestic|international|general，写主为唯一派生函数，tier=filter，index_required=true）；city（derived，string[]，写主为唯一派生函数，tier=filter，index_required=true）；expense_type（chunk 级或 document 级，受控词表：住宿费/交通费/餐费/通讯费/其他，parser 按 heading_path/表格列抽取，admin 覆盖，tier=filter，index_required=true）；employee_level（表格行级，普通/高级/管理/专家/不限，tier=filter，index_required=true，软触发）；effective_date/expiry_date（统一为 date 类型、ISO 序列化，与分层模型一致，tier=filter，index_required=true）；policy_status（硬过滤，查询期默认 current，tier=filter）；acl/tenant_id（tier=permission，index_required=true）。

三、两阶段过滤管线。阶段一查询期过滤：FilterExtractor 把 query 转成 QueryFilters，每个注册表字段有确定性子句抽取（城市/国家/国际关键词/职级/费用/日期），词表外实体才允许 LLM 辅助且必须有确定性 fallback 并回写缓存供审计；显式 filters（trip_intake 目的地、用户所选部门）优先级高于抽取结果，两者取交集（更严格）。权限字段永远在查询期硬过滤。阶段二检索后重校验：Revalidator 对每个进入回答的候选 chunk 按 revalidate_rule 逐条重验（jurisdiction 覆盖查询地点、时效覆盖 valid_on、policy_status 是否 current、expense_type/employee_level 匹配、ACL 放行），不匹配的剔除或降权并在 retrieval_trace 记录 rejected 的字段名、规则与命中值；缺失字段一律宽容放行（旧数据整体视为 general/current/public/null/长期有效）。

四、检索入口扩展。KnowledgeRetriever.search(text, filters=QueryFilters, filter_mode=pre|post|pre+post)，QueryFilters 结构为 text、filters 具名字典、valid_on（默认今天）、filter_mode（默认 pre+post）。返回侧 RetrievalResult.to_dict 增加 applied_filters 与 revalidation 结果。全部过滤经开关 RAG_FILTER_ENABLED 控制（默认 false），关闭时 hybrid_search 原样返回、行为与现状逐字节一致；开关开启后先 post 后 pre+post 分阶段放量。

五、与 §8.3 的触发关系。当前 collection 把 metadata 序列化为字符串存入动态字段，不能直接视为已有可用的结构化过滤 schema。但 Milvus Lite 本身支持 metadata/scalar 过滤，新版 Milvus 也支持 JSON 路径过滤；因此出现 jurisdiction/city/effective_date/ACL 需求时，应先评估在 Milvus Lite 中建立显式标量/JSON 字段和索引，再与 PostgreSQL+pgvector 做基准比较。只有 Lite 在功能、事务、性能或统一运维上无法满足已证实需求时，才触发迁移；不因“需要过滤”本身直接推导迁库。

#### 6.1.5 版本与可观测性

一、索引指纹。index.version = sha256(规范化 JSON：{schema_version, embedding_model, embedding_dimension, embedding_backend, query_instruction, chunk_size, chunk_overlap, chunker_version, parser_versions（按名排序）, tokenizer, code_revision}) 前 16 位十六进制。因子不变则指纹不变（增量入库幂等跳过重建），任一因子变化即指纹变化、触发重建。冻结状态机：pipeline 入口一次性冻结全部因子（版本号由代码常量输出，非现场推导，避免运行中漂移）→ 指纹写入 IngestionReport.metadata 并由 _write_manifest 组装落盘 → 每个 chunk 落库时注入 chunk.index_version。manifest 写失败（如磁盘满）时报错回滚活动版本标记，避免索引已换而 manifest 未更新的中间态。

二、manifest v2 payload。knowledge_base_service._write_manifest 组装 v2：顶层保留 refreshed_at/documents/report（v1 形状原样保留，两处 document_index_status 读取零改动），新增 schema_version、generated_by、index（version/built_at/collection_name/trigger/code_revision/embedding/chunk/parsers/tokenizer）、previous_index（上一次 index 段快照：version/built_at/collection_name）；documents[doc_id] 追加 document_version/parser（名与版本）/pages 终态计数（native/ocr/mixed/skipped/error，修复空页/扫描页被静默跳过）/chunk_count。仍以 tmp 文件加 os.replace 原子替换。

三、可观测链路。retriever 查询时读 manifest 取 index.version 与 collection_name；agent 生成 trace 载荷追加到 data/rag_knowledge/retrieval_traces.jsonl（append-only，按天/大小轮转），字段含 trace_id、schema_version、created_at、index_version（查询时快照）、embedding 快照、query（question/expanded_query/top_k/filters）、results[]（chunk_id/hash/document_id/heading_path/content_type/vector_rank/distance/bm25_rank/bm25_score/fusion_score/rerank_score/final_rank/evidence_gate/page_start）、metrics（candidates/kept/dropped_by_filter/reranked/latency_ms）、answer（status/answer_hash/skill_version）。conversation_messages.answer_document 埋 retrieval_trace_id。

四、检索期字段存储归属。rerank_score/fusion_score/vector_rank/bm25_rank/retrieval_trace_id 不落 Milvus metadata，只存在于 RetrievalResult 与 trace；retrieval_trace_id 写主为查询侧（retriever/hybrid_search 生成，agent 回填卡片）。落库的 chunk 级新增只有 ingested_at/text_source/index_version 三个键，量级可忽略。时间戳命名统一：chunk 级用 ingested_at，manifest 文档级用 indexed_at，禁止同名跨层复用。

五、可复现判定。给定任一回答：answer_document.retrieval_trace_id → 定位 trace → trace.index_version 与每结果 chunk_id。若 trace.index_version 等于当前 manifest.index.version 且 chunk_id 在集合可查，则该回答可用当前索引逐块复现；若已重建（指纹不等）则返回旧版本已下线并给出 previous_index.version 与构建时刻。逐块判定由 chunk.index_version 完成：增量入库后不同文档块可携带不同指纹，精确指出哪些证据来自旧索引。code_revision 依赖 git，非 git 部署缺省 unknown 并纳入指纹降级策略（改用依赖清单哈希或 build tag）；强可复现决策：蓝绿 replace 后旧备份集合被 drop，previous_index.collection_name 只剩记录，若要强可复现需在 drop 前按 previous_index.version 归档最近 N 个备份集合或对象存储快照，并把该决策写入兼容契约。

#### 6.1.6 消费与引用契约

一、类型化序列化。新增 ChunkMetadata dataclass（rag/schemas.py），DocumentChunk.to_metadata() 仍为唯一装配点。ChunkMetadata.to_dict() 输出稳定 JSON：顶层 schema_version 标注契约版本 rag.v2.metadata.1，以下按 document/page/chunk/operational 分组展开为平铺键，并对旧调用方实际读取的键额外派发兼容别名（source/file_path=source_path、file_name/parent_doc=filename、page=page_number、section=heading_path 以 / 连接串）。from_dict() 同时接受 canonical 与旧别名键并归一化为 canonical 字段。字段缺失落到 dataclass 默认值，保证旧行可读。

二、RetrievalResult 变更。新增一等字段 rerank_score（float，可选）与 retrieval_trace_id（str，可选），to_dict() 补发二者并在顶层补发 chunk_id（供去重键使用，additive）。一期 RetrievalResult.metadata 保持 dict：_result_from_dict 用 from_dict() 返回 dict（或让 ChunkMetadata 实现 Mapping/__getitem__），to_dict 输出纯 dict，避免 dataclass 不可下标的破坏（test_rag_production_pipeline 的 metadata['filename'] 式读取不崩）。milvus_store.rerank_results 计算的 rerank_score 不再被丢弃：_result_from_dict 补读 rerank_score 与 retrieval_trace_id；InMemoryVectorStore 复刻相同打分函数（ngram/focus 加权）保证两后端排序依据一致，以黄金查询集做一致性断言。

三、确定性引用投影。_serialize_source 不再猜 fallback 键：file=filename、page=page_start（缺省回退 page_number）、section=一期输出兼容别名连接串（heading_path 以 / 连接，保持字符串语义，二期再切列表；同批修改 fallback_composer 与 check-trip-compliance 对 section 的 str 拼接读取）、excerpt=display_text 前 200 字截取（读端派生，不单独入库）、bbox、table、chunk_id。`document_id/source_path` 仅用于内部 trace 和回源。_serialize_doc 白名单投影需保留 top-level file/page/section（或兼容别名）并同步更新 check-trip-compliance._normalize_sources，避免旧读取端落空回退到企业差旅知识库。_retrieve_for_question 去重键改为顶层 chunk_id，替代当前恒为 None 的顶层 source/file 死读。_format_knowledge_context 注入 display_text 与政策元数据块（jurisdiction/effective_date/policy_status/category）。

四、sources[] 输出契约。运行时以 jsonschema 校验，schema 文本如下（无分页文档的 page 为 null；若后续需要 sheet/row 等定位，新增独立 `location` 字段，不过载 page）：

    {
      "$schema": "https://json-schema.org/draft/2020-12/schema",
      "$id": "https://hommey.local/schemas/rag-answer-output.json",
      "type": "object",
      "properties": {
        "status": {"enum": ["success", "partial", "no_knowledge", "knowledge_base_empty", "error"]},
        "answer": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object",
          "properties": {"text": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
          "required": ["text", "evidence_ids"]}},
        "sources": {"type": "array", "items": {"$ref": "#/$defs/source"}},
        "retrieval_trace_id": {"type": "string"}
      },
      "required": ["status", "answer"],
      "$defs": {
        "source": {
          "type": "object",
          "properties": {
            "chunk_id": {"type": "string"},
            "file": {"type": "string"},
            "page": {"type": ["integer", "null"]},
            "section": {
              "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}}
              ],
              "description": "一期为字符串兼容别名，二期为 heading_path 列表"
            },
            "excerpt": {"type": "string"},
            "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
            "table": {"type": "object", "properties": {
              "sheet": {"type": ["string", "null"]},
              "table_id": {"type": ["string", "null"]},
              "row": {"type": ["integer", "null"]},
              "col": {"type": ["integer", "null"]}
            }, "additionalProperties": false}
          },
          "required": ["chunk_id", "file", "page", "section"]
        }
      }
    }

#### 6.1.7 实施与兼容

一、写路径重写（与 schema 落地同一提交）。重写 rag/loader.py、rag/document_loader.py、rag/chunker.py：去掉 source/parent_doc 对 metadata 的直接写（当前 loader L44 写 metadata={'source','parent_doc'}、document_loader L54-57 相同、chunker L28 setdefault('parent_doc', filename)），改由 dataclass 字段（filename/source_path）与 canonical 键承载；同步更新 tests/test_rag_pipeline.py L22 对 parent_doc 的断言（改读 filename 或删除）。旧 source 键语义废弃后，chunk 级文本来源用新字段 text_source（native/ocr/mixed），避免与旧 source 引用键同名污染现有引用链。

二、兼容层。_coerce_chunk（retriever）扩展为读兼容层：负责旧别名到新字段映射（file_path/source→source_path、parent_doc/file_name/name→filename、page→page_number、section→title→heading_path、chunk_id 旧格式识别、text_source 缺省按 parse_mode 回填或 unknown），旧 collection 与旧 manifest 平滑可读。_read_manifest 增加 _coerce_manifest 只读包装：无 schema_version 的旧文件补合成 {schema_version:1, index:{version:'legacy'}, previous_index:null, generated_by:null}，documents 与 report 原样透传，document_index_status 行为逐字节不变。schema_version 缺省视为 rag.v2.metadata.0（旧九字段），新写入盖章 rag.v2.metadata.1。

三、命名统一裁决。chunk_index 拆为 chunk_ordinal（文档级，不每页重置）与 chunk_index_within_page（页内），当前 chunk_index 每页重置的 bug 随此修复；schema_version 命名空间统一为 rag.v2.metadata.1；excerpt 读端派生、不单独入库（分层模型删除 chunker 写 excerpt 的声明）；table 由 parser 产出 block.table_data、chunker 收敛进 chunk.table；heading_path 由 parser 在 block 层累积、chunker 传播继承。

四、迁移路径。一期兼容别名默认开启并标记 deprecated，agent、filter 全部切换到 canonical 键后于二期关闭别名输出并移除 _coerce_chunk 的 file_path 接受逻辑。section 一期保持字符串连接串，二期切 heading_path 列表，迁移窗口内禁止用字符串语义读写 canonical。title 语义收敛为文档级后，_serialize_source 的 section 回退链改为确定性投影 heading_path 末节加 page_start。V1 collection 原地保留不破坏，改造完成前重建到 V2 staging，跑通黄金查询回归后蓝绿原子切换。

五、落地顺序与验收。阶段一：block 模型 + 分层字段 + chunker 产出 chunk_id + 写路径重写（含测试更新）。阶段二：过滤注册表外部化 + FilterExtractor/Revalidator（开关默认关闭）。阶段三：版本指纹 + manifest v2 + trace + rerank_score 贯通两后端。阶段四：引用投影切换 canonical。验收要求：黄金查询回归通过后原子切换；单测断言每个 catalog 字段至少有一个消费点（检索/引用/审计之一），杜绝新字段成为死重；单测锁定 v1 与 v2 两种 manifest 下 document_index_status 行为不变；两后端（InMemory/Milvus）rerank_score 一致性断言；每个 catalog 字段的单一写主由测试锁死（旁路写键直接报错）。

六、残余风险与对策。parser/chunker/embedding 版本必须由代码常量输出，运行期不得重解析，否则指纹抖动触发无谓重建；重校验只对进入候选的 top-k 生效，过滤不替代召回质量，必须与切块修复、证据门槛并行推进；Milvus metadata 尺寸上限约束下，入库仅写 canonical 加兼容别名最小必需集，超长字段走独立列或子存储；词表不全的误杀风险以管理端/数据文件可编辑词表 + 剔除原因进 trace 兜底，避免静默丢证据。

## 7. 切块策略重构：切块器只消费结构（本期核心）

> 本节是本期改造的**核心**。它不做"在现有正则里加一条 Markdown 规则"这类点状修补，而是把切块从"对裸文本猜结构"重构为"对结构化 block 做归并拆分"。§4.2（孤儿切块）、§4.5（Markdown 结构丢失）、§4.6（规范化破坏表格）、§4.7（身份不稳定）、§3.2（标题碎片污染排序）都是同一类问题在不同格式上的表现，统一由本设计解决。

### 7.1 问题本质

当前链路是：

```text
TxtParser/PdfTextParser → text(str) → TextNormalizer → TextChunker → chunk(str)
```

结构（标题、列表、表格、代码、版面）在进入 `TextChunker` 之前已经被压平成一个字符串。`TextChunker` 只能靠 3 条正则去猜"这一行是不是新话题开头"，猜对了就冲刷当前块——这就是孤儿标题块、Markdown 标题失效、列表碎片化等一系列问题的共同根因。**只要输入还是裸文本，正则再加多少条都救不回来。**

### 7.2 泛化设计：一个契约、七条原则

改造后的链路是：

```text
TxtParser/PdfTextParser/... → blocks[](带 type) → TextNormalizer(按 type) → Chunker(按结构归并拆分) → chunk
```

**原则 P1 — 切块器只消费 block 序列，不消费裸文本。** 解析器负责把文档拆成 §6 的 block 序列（`heading/paragraph/list/table/code/...`）。切块器在 block 序列上工作，不再对整篇裸文本猜结构。新增格式应优先通过新解析器复用公共归并/拆分逻辑；若引入新 block type 或特殊跨页规则，允许以可测试的分派扩展切块器。

**原则 P2 — 标题是路径，不是叶子。** 标题 block 永不单独成块。它被压入一个 heading stack，累积成 `heading_path`（如 `["一、交通标准", "（一）高铁"]`）附加到其后每个正文块。**不变式：任何叶子 chunk 必须包含至少一个非标题 block**，除非该"标题"本身是完整的可回答记录（如 `Q1: 发票丢了怎么办 → 答：...`，此时它是 paragraph 不是 heading）。

**原则 P3 — 先合并后拆分，取代"遇到标题即冲刷"。** 现有机制是"检测到新话题 → 冲刷当前块"，这是碎片化的直接来源。新机制：先把同一 `heading_path` 下的 block 累积成"逻辑段"，再把相邻短逻辑段（不足最小 token 数）与同父路径的段合并，只对**超长**段在句子/列表边界拆分。字符窗口只作为最后手段。

**原则 P4 — 主题识别是"规则注册表"，不是单条正则。** 把现在硬编码在 `chunker.py` 的 3 条正则收敛为注册表：每条规则含正则、匹配优先级、标题层级、适用格式。新增格式 = 注册一条规则，不修改切块器。这是 P1 的配套：即使某些格式只能给"文本 + 推测的类型"，也通过注册表把推测逻辑隔离在解析器/规则层。

**原则 P5 — 按 block type 规范化。** 段落 block 压空白；代码/表格/OCR 版面 block 保留原始空白。`TextNormalizer` 不再对整篇文本做一次性替换。

**原则 P6 — 身份从文档谱系派生。** chunk 身份 = `document_id / version / page / block_id / chunk_ordinal`，在相同文档版本、parser/chunker 版本下跨重建稳定；幂等键 = `chunk_hash + document_id + document_version`，不以部署机器的绝对 `source_path` 为跨环境身份。§4.7 的每页重置 `chunk_index`、重复 `chunk_id`、§4.3 的增量重复，统一由这一条解决。

**原则 P7 — 按 token 计大小，参数可配置。** 用 tokenizer（而非字符数）度量 chunk 大小。建议初值 `min=150 / max=400 / overlap=60` token，通过评测标定，不写死成"行业最佳值"。

**原则 P8 — 每个原始页面有终态。** `native_text | ocr_text | intentionally_skipped | error`，任何页面不得无记录消失（承接 §4.1）。这是所有"丢页"类问题的统一出口，OCR 只是其中一个分支。

### 7.3 与现有问题的映射

| 现有问题 | 由哪条原则解决 |
|---|---|
| §4.2 孤儿标题块 / §3.2 标题碎片占槽位 | P2（标题是路径）+ P3（合并短段） |
| §4.5 Markdown/表格/PDF 结构未参与切块 | P1（只消费结构）+ P4（规则注册表） |
| §4.6 规范化压平表格/代码 | P5（按 type 规范化） |
| §4.7 chunk_id 不稳定 / §4.3 增量重复 | P6（谱系身份 + 幂等键） |
| §4.1 扫描页静默丢失 | P8（页面终态） |
| §4.12 引用无真实 section/block | §6 block 模型为引用提供 block_id/heading_path/bbox |

### 7.4 实现要点（泛化）

- 引入 `Block` dataclass（type、text、heading_path、level、bbox、table_data），`ParsedDocument.blocks` 成为解析器新契约；`text` 字段保留为 `blocks` 的渲染结果，兼容旧调用方。
- `TextChunker` 重构为在 `blocks` 上操作；`_starts_new_topic` 的冲刷逻辑删除，由 P3 的归并拆分取代。
- 主题规则注册表放在独立模块（如 `rag/heading_rules.py`），内置中文法规编号、Markdown ATX/setext、`第X章`、`（一）`、`1、`、`Q1:`、列表项等规则，后续格式在注册表中追加。
- 增量入库统一走"先 hash 幂等，后按 doc_id 切换版本"，蓝绿机制保留。
- 规范化器改为按 block type 分派。

### 7.5 迁移与兼容

- V1 collection 保留，不原地改：改造完成前重建到 V2 staging collection，跑通黄金查询回归后再原子切换（沿用现有蓝绿机制）。
- 以 `chunker_version` / `parser_version` 触发重建；manifest 记录实际生效版本。
- 目标：相同文件重复增量入库新增 0 块；heading-only 证据块为 0；chunk_id 在相同文档和 parser/chunker 版本下跨重建稳定；所有原始页面有明确终态。

## 8. 存储决策：Milvus Lite vs PostgreSQL（本期结论：保留 Milvus Lite，不迁移）

> 本节回答审计中提出的三个问题：① Milvus 与 PostgreSQL 数据是否冲突；② Milvus 对当前项目是否过重；③ 是否应把 RAG 存储迁移到 PostgreSQL。

### 8.1 现状事实

- **Milvus 用的是 Lite（嵌入式），不是 Standalone。** 数据落在 `data/rag_knowledge/milvus_lite.db`（约 3.7 MB），由应用启动嵌入式引擎，无需独立部署服务或 etcd。它被用于两件事：dense 向量 ANN 检索 + 作为 BM25 的原始数据源（Python 全量扫描，见 §4.9）。
- **PostgreSQL 已是 Web 业务栈的一部分，但未承载 RAG 向量。** `HOMMEY_LONG_TERM_BACKEND=file` 只表示长期记忆后端默认为文件，不代表 PostgreSQL 对整个 Web 应用都是可选的：鉴权存储直接读取 PostgreSQL DSN，聊天附件元数据和 extraction 也使用 PostgreSQL。项目已有 17 个 checksum 校验的 migration，**但尚未使用 pgvector 扩展**，迁移文件中没有向量列。
- **两者当前没有数据重叠，也不存在"冲突"。** Milvus 存的是"chunk 文本 + 向量 + metadata"；PostgreSQL 存的是"用户、会话、记忆、附件等业务状态"。RAG 的 category/tenant/有效期/ACL 等字段将来可选择留在 Milvus 标量/JSON 字段，也可选择放入 SQL，需按过滤、事务和运维需求决策。
- **存储层已经有一层抽象：`rag/vector_store.py` 的 `VectorStore` ABC**，且带 `InMemoryVectorStore`（测试用）与 `MilvusVectorStore` 两个实现，`replace_chunks` 抽象出了"原子替换"契约。这为换后端提供了接缝，但不意味着只需新增一个类：原生 hybrid search、标量过滤、upsert 和发布事务仍需要后端级契约测试和少量上层适配。

### 8.2 逐一回答

**① Milvus 与 PostgreSQL 冲突吗？** 不冲突。它们存不同数据、服务不同用途，可以长期共存。两个存储的成本包括备份/恢复需要覆盖两处、发布一致性需要单独设计，以及当前 RAG 元数据尚无 SQL 查询面；这些是取舍，不是数据冲突。

**② Milvus 对当前项目重吗？** 不重——当前用的是 **Lite**，嵌入式、文件型、无需独立服务，数据约 3.7 MB。真正"重"的是 Milvus Standalone/Distributed（独立服务 + etcd + 运维成本），原文档 Phase 5 的"升级到 Milvus Standalone/Distributed"才是重选项，而这个选项**本期明确不做**。以 384 chunks 的语料，Lite 的检索能力绰绰有余，瓶颈在切块质量与 BM25 全表扫描（算法问题，见 §4.9），不在向量存储本身。

**③ 迁移到 PostgreSQL 会更好吗？** 现阶段**不会更好**。可行，但收益与成本当前不匹配：

- 收益：单一存储、SQL 元数据过滤（ACL/tenant/有效期直接 WHERE）、事务化替换、备份统一。
- 成本：pgvector 需要 PostgreSQL 服务器安装扩展；中文 BM25 迁移到 PG 需要 tsvector + 中文分词方案，比现在的 Python BM25 更复杂、更依赖环境；迁移要重写 `MilvusVectorStore` 为 `PostgresVectorStore`，蓝绿机制改为 staging table + rename（`replace_chunks` 契约可平移，但工程量大）；且迁移解决的是"存储"问题，不会自动修复当前的页面丢失、切块和引用 Bug。

**结论**：**保留 Milvus Lite，本期不做存储迁移。** 保留 `VectorStore` ABC 作为接缝，并补齐需要的过滤/upsert 契约；将来迁移仍应作为独立工程评估，不将其低估为纯配置切换。

### 8.3 何时才值得迁移（触发条件，不是计划）

迁移不是按时间排期，而是按下述任一条件触发时重新评估：

1. **语料规模显著增长**（`10 万 chunks` 只是评估示例，不是无基准支撑的硬阈值），或全量重建/检索 p95 已超出产品 SLO，Python BM25 全表扫描或 Lite 成为经 profile 证实的瓶颈；
2. **出现硬性的元数据/权限过滤需求**，且经 Milvus Lite 显式标量/JSON schema 的 PoC 后，在事务语义、查询表达或性能上仍不能满足；
3. **运维要求单库统一**（备份、审计、迁移便利性优先）。

若触发，将 **Milvus Lite schema 扩展、PostgreSQL + pgvector，以及必要时的托管/独立向量服务**放入同一基准比较，不预先宣布唯一胜者。PostgreSQL 已在业务栈内，因此 pgvector 是高优先级候选；但中文 lexical search、向量索引、发布事务和运维 SLO 仍需实测。BM25 无论落在哪个存储，都应在语料扩大前从"逐查询全表计算"迁出。

### 8.4 对 §12 路线的影响

- Phase 0-2 完全不涉及存储选型，聚焦数据质量。
- Phase 3（OCR worker）与存储解耦：解析产物先写 staging，发布走 `VectorStore.replace_chunks`。
- 存储评估放在语料/需求触发时，而不是按时间排期；`VectorStore` ABC 与后端契约测试将大部分切换成本隔离在存储适配层，但不承诺业务层零改动。

## 9. 检索层修改方向

### 9.1 本期必交与后续候选

本期必交：

1. 实现 §7 的最小 block 模型与归并式切块，消灭 heading-only 证据块。
2. 建立稳定 ID、幂等去重与最小版本信息（P6）。
3. 修正 `sources[].file`、page 和 excerpt 的引用投影。
4. 建立种子黄金查询集，固化当前 baseline 和可复现的检索 trace。

后续候选（对应 Phase 4，不列入本期承诺）：通用 reranker/evidence classifier、no-knowledge 阈值、以及 exact entity、金额、日期、否定词和适用范围的 metadata/filter 约束。这些项目只有在基线表明具体收益时才启用。

### 9.2 混合检索

- 原始 query 必须始终保留 lexical 分支，以保护精确术语、文件编号、金额和地名。
- 中文 lexical tokenizer 可保持现有 2/3/4-gram ngram 化（已比逐字方案改善），但应为未来的领域词典/可版本化分词留接口。
- **BM25 迁移时机**：与 Phase 顺序保持一致——语料尚未扩大到 Python 全表扫描成为瓶颈前，不提前迁移；一旦触发 §8.3 的规模条件，先做预计算 BM25/原生 sparse，再评估存储（见 §4.9）。
- 候选融合后再用通用 cross-encoder rerank；可以先评估 BGE rerank 系列，但模型选择必须以本地制度数据评测为准。
- 用 MMR/near-duplicate 过滤避免同一条款的重复 chunk 占满 top-k。
- 多事实问题应动态决定 evidence 数量，不应始终 `top_k=3`。

### 9.3 范围和时效过滤

政策类 RAG 应至少支持：

- 国内/国际、国家、城市和地区；
- 员工等级/部门/合同主体；
- 费用类型；
- 生效日期、失效日期、当前状态；
- 公开范围、tenant 和 ACL。

检索前从问题中抽取 filter，检索后仍校验范围冲突。不能只靠 LLM 阅读三个文本块自行判断。这些过滤若成为硬需求，是 §8.3 存储评估的触发条件之一。

### 9.4 重排与证据门槛（泛化机制）

- **重排（scoring）**：保留轻量的 ngram/focus 加权作为第一层；把 `rerank_score` 写入 `RetrievalResult` 与 `to_dict()`（当前被丢弃），使排序依据可见、可测。
- **门槛（gating）**：证据门槛必须是**对所有查询生效**的通用机制——得分低于语料标定阈值的证据不得进入回答，无论查询含不含"餐费"。`filter_relevant_results` 的"rank 1 无条件保留"规则移除或改为"仅当分数显著领先时保留"。
- 门槛与回答框架联动：无足够证据时返回 `no_knowledge` / `partial`（见 §10）。

## 10. 回答与引用框架修改方向

建议将 RAG 输出收紧为（状态枚举与 Skill schema 统一，含 §4.13 的 `knowledge_base_empty`）：

```json
{
  "status": "success | partial | no_knowledge | knowledge_base_empty | error",
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

- 每个数值、日期、比例、交通等级和"可/不可报销"结论必须关联至少一个真实 evidence ID。
- 引用的 evidence 必须属于当前检索结果且通过权限过滤。
- 回答中的地区/国内国际范围不得与证据 metadata 冲突。
- 无足够证据时返回 `partial` 或 `no_knowledge`，不靠措辞掩盖证据缺口。
- 对 Skill 输出启用运行时 JSON Schema 校验（§4.13 的潜在缺陷在启用校验前必须先统一状态机）。

## 11. 分阶段改造路线

> 与上一版相比：HyDE 仍不属于一期承诺，但在一期基础能力基本完成后，以 Phase Y 为 Go / No-Go 分水岭，进入独立的 Phase 6 实验与上线链路；Phase 5 的存储升级仍按 §8.3 条件触发评估，默认保留 Milvus Lite、不预设 Standalone。

### ~~Phase 0：建立可比较基线~~

- ~~先建立 30～50 条人工标注的种子黄金查询，覆盖精确问法、口语同义、否定、城市范围、多事实和无答案；随真实失败案例扩展到 100 条以上。尚未支持的表格/OCR 问题单独作为后续阶段数据集，不混入 Phase 1 改造判定。~~
- ~~保存当前 384 chunk 的 baseline 检索结果与回答结果。~~
- ~~种子集先记录 Recall@5/10、MRR@10、no-knowledge precision、citation file/page 正确率和 p50/p95 延迟；待相关性分级标注成熟后再引入 nDCG 和 claim-level citation coverage。~~
- ~~在 manifest 中加入 schema/parser/chunker/embedding/index 版本。~~
- ~~增加检索 trace，保留所有分支评分和过滤原因。~~

~~完成条件：任何后续方案都能与同一数据集进行 A/B，而不是凭主观感受上线。~~

### ~~Phase 1：先修数据质量和幂等性（本期核心交付）~~

- ~~引入一期最小结构化 block 模型（§6），TXT/Markdown/PDF 文本层解析器统一产出 block 序列。~~
- ~~实现归并式切块（§7 的 P1-P8），消灭 heading-only 块、不必要的碎片化和 Markdown 结构丢失。~~
- ~~修复增量去重与稳定 chunk 身份（P6）：相同文件重复入库新增 0 块。~~
- ~~统一文本编码策略。~~
- ~~规范化器按 block type 分派（P5）。~~
- ~~修正 `sources[].file` 被固定 `source` 覆盖的问题，并输出可复核的 page/excerpt。~~
- ~~增加 `parser_version/chunker_version` 触发重建。~~
- ~~对当前语料重建 V2 staging collection，通过回归后原子切换。~~

~~完成条件：相同文件重复增量入库新增 0 块；没有 heading-only 证据块；chunk_id 在相同文档/parser/chunker 版本下跨重建稳定；所有原始页面有明确终态；`sources[].file` 为真实 filename；manifest 记录生效版本。~~

### ~~Phase 2：扩展 DOCX、CSV、XLSX 和表格~~

- ~~知识库上传、后端 parser registry、前端提示和预览统一开放格式。~~
- ~~各格式解析器产出 §6 block 序列，优先复用通用切块逻辑；仅对新 block type 增加显式、可测试的分派。~~
- ~~DOCX 保持 paragraph/table 原始顺序。~~
- ~~CSV/XLSX 形成 sheet/table/cell 结构和三重索引表示。~~
- ~~表格引用可定位到 sheet、table、row/cell。~~
- ~~对公式、合并单元格、空表头、超宽/超长表建立边界测试。~~

~~完成条件：表格数值问答能够引用原始单元格，不依赖表级摘要生成数值。~~

~~落地记录（2026-08-12）：新增 `rag/structured_parser.py`（DocxParser/CsvParser/XlsxParser），DOCX 走 body XML 保持 paragraph/table 原始顺序、原生 Heading 样式补充 heading_path；CSV/XLSX 形成 sheet/table/cell 结构；`Block.table_data`/`ParsedDocument.location`/`DocumentChunk.table` 承载 sheet/table_id/row/col 引用；超长表格在 chunker 按行分带；`HOMMEY_RAG_SUPPORTED_FILE_TYPES` 默认含 docx/csv/xlsx，上传校验支持新格式。测试见 `tests/test_rag_phase2_tables.py`。~~

### ~~Phase 3：OCR 与版面解析~~

- ~~原生文字快路径 + 页面级 OCR/layout fallback。~~
- ~~可复用现有 VisionClient（Qwen2.5-VL）作为图片/扫描页的视觉前端，与聊天附件图片识别共用一套视觉能力。~~
- ~~显式记录空页、低置信度、超时、密码和损坏状态（§7 P8 的终态落到 worker 日志）。~~

~~完成条件：扫描页要么可检索，要么在管理端显示具体失败原因；不得静默消失。~~

~~落地记录（2026-08-12）：新增 `rag/ocr.py` 的 `PageOcrFallback`，flag 门控（`HOMMEY_RAG_OCR_ENABLED`，默认关），复用 `multimodal/vision_client.py`；无文字 PDF 页经 pymupdf 渲染后走视觉模型，成功且置信度达标 → `ocr_text`，低置信度/空 OCR → `intentionally_skipped` 并记录原因，渲染或调用失败 → `error` 并记录原因；OCR 开关与置信度阈值进入索引指纹。测试见 `tests/test_rag_phase3_ocr.py`。~~

~~延后项（明确不排期）：独立 document worker 与任务状态表、PaddleOCR PP-StructureV3 / Docling PoC、页眉页脚/多栏/旋转/印章/水印/无框表格回归集——后续按需以扩展点补入。~~

### Phase 4：通用重排与证据门槛（不再含 HyDE）

~~- 把 ngram/focus 重排作为第一层保留，`rerank_score` 写入 `RetrievalResult`。~~
~~- 引入通用 reranker 或最小可用 evidence classifier，门槛对所有查询生效。~~
~~- 建立 no-knowledge / partial 判定并纳入回答框架。~~
~~- HyDE 不进本阶段；若未来需要，按附录 A 设计单独评估。~~

~~完成条件：无依据查询稳定返回 `no_knowledge`/`partial`；弱证据进入回答的比例显著下降。~~

~~落地记录（2026-08-12）：新增 `rag/evidence.py` 的 `evaluate_evidence` —— 无 LLM、无外呼的确定性通用证据门槛，对所有查询生效。信号取两个无量纲量：证据覆盖率（CJK 2-gram + 拉丁词 + 领域 rerank 词；修复 3–4 gram 对"报销流程"等短查询覆盖率归零的问题），以及 rerank 提升值（`rerank_score − fusion_score`，跨 Milvus RRF 与内存库同一尺度）。判定：覆盖率 ≥0.40，或 ≥0.25 且提升 ≥0.15 → `sufficient`；覆盖率 <0.05 → `insufficient`；其余 → `partial`。用 `tests/data/golden_queries.json`（36 条、6 意图）对 `data/documents` 真实语料标定：全部 no_answer 均判 `partial`（高 rerank 分证明 rerank 不能单独放行），有据可答查询无一判 `insufficient`。ask-question agent 接线：`insufficient` → `no_knowledge`（附带 evidence 原因）；`partial` → 新增 `partial` 状态 + 兜底措辞注入回答框架（片段未直接回答时必须如实说明、不得编造）；`sufficient` → `success`；`output.json` 状态枚举补齐 `partial`/`error`/`knowledge_base_empty`。测试见 `tests/test_rag_phase4_evidence.py`。~~

### Phase 5：检索基础设施扩展（按触发条件评估，非排期）

~~- 语料扩大到 Python 全表扫描成为瓶颈时，先迁移 BM25 为预计算/原生 sparse。~~
- 出现硬性元数据/权限过滤需求时，先对比 Milvus Lite 显式标量/JSON schema 与 PostgreSQL + pgvector（§8.3）。
~~- 增加 embedding cache、有限重试、批量本地 encode。~~
- **不预设升级 Milvus Standalone**；存储选型由 §8.3 触发条件决定。

~~落地记录（2026-08-12）：embedding 侧补齐 §4.15 的重试与退避（指数退避，仅瞬时 5xx/429/连接超时重试，4xx 鉴权错误不重试）与进程内 LRU 缓存（同文不重复付费调用，返回副本防污染），开关经 `HOMMEY_RAG_EMBEDDING_MAX_RETRIES`/`RETRY_BASE_DELAY_SEC`/`RETRY_MAX_DELAY_SEC`/`CACHE_SIZE` 配置。BM25 侧把全表扫描 Python 实现从 `MilvusVectorStore.bm25_search` 迁移到 `rag/sparse.py` 的 `SparseIndex` 接缝（`PythonBM25SparseIndex` 分数与旧内联实现逐位一致），`HOMMEY_RAG_BM25_BACKEND` 选型，原生 sparse/预计算倒排索引是未来触发项、只差一个配置值；元数据/权限过滤与 Milvus Standalone 仍按 §8.3 触发条件评估，本期未实施。测试见 `tests/test_rag_phase5_resilience.py`。~~

---

### Phase Y：HyDE 分水岭（一期基础工程结束 / 二期召回增强开始）

> `Y` 是准入门（gate），不是排在 Phase 5 与 Phase 6 之间的新功能阶段。Phase 5 中按规模触发的存储/权限项不阻塞 HyDE；只有与 HyDE 安全性和可评测性直接相关的条件必须全部满足。

进入 Phase 6 前必须同时满足：

- Phase 0/1/4 的黄金集、稳定 `chunk_id`、检索 trace、通用 evidence gate 和 `success/partial/no_knowledge` 状态机已在线可用。
- 当前生产索引不存在未关闭的 S1 数据完整性或引用归因问题；合成文本与真实文档在数据模型中可以明确区分。
- 冻结一份 HyDE 实验集，至少标出 `hyde_candidate`（口语/情境描述）、`exact_query`（金额/日期/政策号）、`no_answer` 和安全负例四类。
- 先完成不调用 LLM 的确定性 query rewrite 对照组；若它已解决目标失败样本，则对应样本不启用 HyDE。
- 预先登记 Go / No-Go 指标和延迟、调用量、成本预算，禁止看到实验结果后再改变成功口径。

任一条件不满足即停在 Phase Y，不得以“先全量打开再观察”替代准入。Phase Y 通过只表示可以开始受控实验，不表示 HyDE 已获准进入回答链路。

---

### Phase 6a：冻结实验协议与对照组

- 从现有黄金集和真实失败 trace 中建立 `tests/data/hyde_queries.json`；每条记录包含查询类别、相关 chunk ID、是否允许 HyDE、应跳过原因和期望回答状态。
- 固定三个对照臂：当前标准检索、标准检索 + 确定性 rewrite、用户显式选择的增强检索。所有实验使用同一索引版本、top-k、reranker 和 evidence gate。
- 先报告分组后的 Recall@5/10、MRR@10、no-knowledge precision、duplicate-evidence rate、p50/p95 延迟、LLM 调用率和单查询增量成本，不只看全量平均值。
- 将以下规则写成实验清单：产品默认始终为标准检索；只有用户显式选择增强检索才生成 HyDE。非差旅域不会调用 `ask-question`，权限校验失败、空输入和提示注入命中时即使用户选择增强也禁止生成并回退标准检索。

完成条件：实验集、基线结果、预算和 Go / No-Go 口径进入版本控制，同一命令可重复生成对比报告。

### Phase 6b：引入 QueryBundle 与用户显式模式选择

- 新增独立的 `QueryBundle` / `QueryVariant` 模型，至少承载 `text`、`kind`、`weight`、`synthetic`、`prompt_version` 和 `skip_reason`；不把 HyDE 逻辑塞进现有 `expand_query()`。
- 保持当前原始 query 与确定性扩展行为不变，先将它们适配为 bundle 的基线分支，再增加 `hyde_passage` 可选分支。
- Web 端提供会话级模式选择器：`标准检索（速度快且稳定）`与`增强检索（HyDE 辅助召回，提高准确率但会增加少量等待时间）`。新会话默认标准；用户选择增强后在该会话保持，直到手动切回。
- 后端不根据置信度自动替用户切换模式。`retrieval_mode=enhanced` 只影响本轮实际执行的 `ask-question` RAG 节点；天气、火车、偏好等其他节点保持原行为。行程规划内部若调用制度查询，则该制度查询继承本轮模式。
- 模式决策写入 trace，包括 `requested_mode/effective_mode/status/fallback_reason`；增强失败时自动回退标准检索，并在回答旁显示“增强未完成 · 已使用标准检索”。

完成条件：未传字段或 `retrieval_mode=standard` 时结果与改造前逐条一致；只有用户显式选择增强时才产生 HyDE LLM 调用。

### Phase 6c：实现受约束的 HyDE 生成器

- 新增可替换的生成器接口（建议 `rag/hyde.py`），输入只包含规范化查询和必要的 policy scope，输出 `HyDEResult(text/model/prompt_version/latency/error)`；不得读取召回文档后再反向生成“假想文档”。
- 使用附录 A.4 的 prompt 作为 v1，并增加确定性输出校验：若生成内容引入用户问题中不存在的金额、比例、日期、城市等级、审批人或确定性报销结论，则丢弃该分支并无损回退，不把违规文本送去 embedding。
- 限制单次生成、最大字符数、超时和总调用预算；超时、限流、空结果、格式错误和安全校验失败统一返回可观测的 fallback，不影响原始检索。
- 缓存键使用 `normalized_query + model + prompt_version + policy_scope`；缓存设 TTL、容量和敏感数据策略，不缓存权限范围不明确的请求。
- 单元测试覆盖 prompt 注入、凭空金额/日期、超长输出、超时、429/5xx、空响应和 cache scope 隔离。

完成条件：故障注入下 100% 回退到原始检索；HyDE 文本进入 evidence、引用或回答上下文的次数为 0。

### Phase 6d：接入 dense-only 分支、融合与追踪

- `hyde_passage` 只做 dense embedding/search，绝不进入 BM25、lexical query 或回答 context；原始 query 继续同时走 dense + lexical。
- 所有分支按稳定 `chunk_id` 去重后再做加权 RRF；初始实验权重沿用附录 A.1 的候选范围，但权重必须由 Phase 6a 实验选定，不能硬编码成产品规则。
- 对每条 query 设置最大 variant 数、每分支候选数和总候选预算，避免“rewrite 数 × HyDE × top-k”乘法膨胀。
- 扩展 retrieval trace：记录原始/规范化/HyDE 各分支候选、分支 rank、融合贡献、cache hit、生成耗时、embedding/search 耗时和最终 evidence gate 结果；合成文本标记 `synthetic=true` 并按敏感数据策略存储或哈希化。
- evidence gate、reranker 和回答生成器只接收融合后命中的真实 chunk；禁止把 HyDE passage 伪装成 rank 0 文档或 citation。

完成条件：关闭 HyDE 可一键回到当前检索；任一最终引用都能回溯到真实 `document_id/chunk_id`，且 trace 能解释 HyDE 是否改变了最终排名。

### Phase 6e：离线 A/B 与 Go / No-Go 决策

- 运行 Phase 6a 的四臂对比，并按查询类型单独出表，重点检查“发票丢了怎么报销”等词面错位样本，而不是用精确金额类的高基线结果稀释收益。
- 推荐候选 Go 条件：`hyde_candidate` 子集 Recall@10 相对确定性 rewrite 有稳定提升（首轮可观察 `+5` 个百分点或 MRR@10 `+0.03`），全量检索质量不下降，no-answer 误放行不增加，合成证据泄漏为 0。候选数值必须根据样本量和置信区间复核，不直接作为长期 SLO。
- 同时报告代价：p95 端到端延迟、每查询 LLM 调用率、cache 命中率、token 与金额成本。任一项超过 Phase Y 登记预算即 No-Go，即使召回指标上涨也不得进入灰度。
- 对收益样本做消融：确认增益来自 HyDE，而不是 query rewrite、候选数增加或权重变化；若确定性 rewrite 达到相同效果，优先采用更便宜、更可解释的方案。

完成条件：形成一份可复现的决策记录，只允许 `No-Go/继续离线迭代` 或 `Go/作为用户可选模式上线`，不得把 HyDE 设为默认检索。

### Phase 6f：可选模式上线、观测与回滚

- 标准检索始终为默认路径；增强检索作为用户可选模式上线，不做后端自动全量开启。观测按用户明确选择增强的请求单独统计，不用标准请求稀释延迟与错误率。
- 产品模式只有 `standard/enhanced`；另保留 `HOMMEY_RAG_HYDE_ENABLED` 运维总开关。总开关关闭时，前端增强请求仍可被识别，但后端必须回退标准并返回可展示状态。
- 自动回滚条件至少包括：no-knowledge precision 恶化、错误/超时率越界、p95 或成本超预算、出现任何 synthetic evidence/citation 泄漏。回滚只关闭 HyDE 分支，不切索引、不影响基础检索。
- 上线后持续保留对照桶和按类别指标；路由规则、prompt、模型、权重任一变化都提升版本并重跑 Phase 6e，不允许静默热改。

完成条件：默认标准检索；用户选择增强后该会话持续生效；可不重建索引关闭 HyDE；连续观测期内满足 Phase Y 登记指标且没有合成证据泄漏。

落地记录（2026-08-12）：聊天请求新增向后兼容的 `retrieval_mode=standard|enhanced`；首页与对话输入区新增会话级检索模式菜单，新会话默认标准，浏览器按用户与 session ID 保存选择。模式经 manager/request context 只传入 `ask-question`；新增 `rag/hyde.py` 的输出硬事实校验、dense-only 分支、稳定 chunk 去重与加权 RRF。生成超时、运维关闭、模型错误、空结果、违规金额/日期/政策结论均自动回退标准。回答卡显示“增强检索”或“增强未完成 · 已使用标准检索”；独立 trace 记录 request ID、模式、fallback reason、模型/prompt 版本、耗时、候选数、selected chunk IDs 及 query/output hash，不落原始 query 与假想文本。

## 12. 建议的功能开关与回滚点

> 以下是跨阶段开关目录，不代表所有开关都要在 Phase 1 实现。一期仅需覆盖 index version、structured parsing/block chunking 和必要的新旧索引切换。

```text
RAG_INDEX_VERSION=v1|v2
RAG_STRUCTURED_PARSING_ENABLED=false
RAG_BLOCK_CHUNKING_ENABLED=false        # §7 泛化切块的开关
RAG_TABLE_PARSING_ENABLED=false
RAG_OCR_ENABLED=false
RAG_OCR_ENGINE=paddleocr|docling|none
RAG_VISION_CLIENT_ENABLED=false          # 复用聊天视觉模型做扫描页 OCR 的前端
RAG_RERANKER_ENABLED=false
RAG_EVIDENCE_GATE_ENABLED=false
RAG_STORE_BACKEND=milvus_lite|in_memory  # VectorStore ABC 的选型开关
HOMMEY_RAG_HYDE_ENABLED=true              # 运维能力总开关；产品请求默认仍是 standard
# ChatRequest.retrieval_mode=standard|enhanced  # 用户会话级选择，缺省 standard
HOMMEY_RAG_HYDE_TIMEOUT_SEC=<seconds>
HOMMEY_RAG_HYDE_MAX_CHARS=<chars>
HOMMEY_RAG_HYDE_CANDIDATE_TOP_K=<count>
HOMMEY_RAG_HYDE_RRF_WEIGHT=<offline-calibrated>
HOMMEY_RAG_HYDE_PROMPT_VERSION=hyde-policy-v1
HOMMEY_RAG_HYDE_TRACE_FILE=data/rag_knowledge/hyde_traces.jsonl
```

回滚策略：

- V1 与 V2 collection 并存，不覆盖唯一活索引。
- 所有重建先写 staging，校验行数、版本、随机抽检和黄金查询后再切换 alias/collection。
- manifest 记录当前 active index version 和上一版本。
- 泛化切块/证据门槛均可单独关闭，关闭后退回原有行为。
- 解析产物按 document hash + parser version 缓存，回滚代码不删除原始上传文件。
- OCR 失败不触碰当前线上索引。
- 存储后端经 `VectorStore` ABC 切换，回滚只需切回 `milvus_lite`。

## 13. 验收指标建议

以下分为硬正确性门槛和待基线校准的质量目标。除静默丢页、重复 chunk、heading-only 证据块、ID 稳定性和引用文件归因等硬门槛外，百分比目标均为初始候选，必须在 Phase 0 完成基线后确认，不能将未标定数字直接当作上线 SLO。

### 13.1 解析与切块

- 100% 原始页面具有解析终态；静默丢页数为 0。
- 相同文档版本重复入库，重复 chunk 为 0。
- heading-only 叶子块为 0，除非标题本身就是独立可回答记录。
- chunk ID 全局唯一，且在相同文档/parser/chunker 版本下跨重建稳定。
- 结构化表格中金额、单位、表头和合并关系可追溯到原单元格。

### 13.2 检索

- 黄金集检索质量不得低于 Phase 0 baseline；Recall@10 ≥ 95%、MRR@10 ≥ 0.80 作为首轮候选目标，由种子集难度复核。
- 精确金额/日期/政策编号查询 top-3 命中率和 no-knowledge precision 必须单独报告；95%/90% 是候选目标，不是已证实 SLO。
- 近重复块应按查询报告 duplicate-evidence rate；不预先把 40% 作为普遍合理分界。
- `rerank_score` 出现在检索 trace 与 `RetrievalResult.to_dict()` 中。

### 13.3 回答与引用

- 数值/资格/审批/时限类 claim 的 evidence coverage = 100%。
- Phase 1 中引用的 `file` 必须 100% 来自真实 filename，不得输出固定类别常量或服务器绝对路径；page/section/block 准确率的比例目标由基线后确认。
- 地区或国内/国际范围冲突导致的错误套用为 0。
- 低置信 OCR 证据若被引用，回答必须携带核对提示。
- Skill 输出通过 JSON Schema 校验（状态机含 `knowledge_base_empty`）。

## 14. 必补测试清单

### 单元测试

- Block 模型：heading/paragraph/list/table/code 各类型的构造与渲染；heading_path 累积。
- 泛化切块：标题永不单独成叶子的不变式；相邻短段合并；超长段按句子/列表边界拆分；代码块不被中间切断。
- 中文法规编号注册表全覆盖：`第X章`、`一、`、`（一）`、`1.`、`1、`、`Q1:`、Markdown ATX/setext。
- 按 block type 规范化：代码/表格空白保留，段落空白折叠。
- 幂等入库：同 hash 重复写入新增 0 块；文档版本替换；并发写入。
- PDF 空页、扫描页、混合页、加密、损坏、多栏、旋转、页眉页脚。
- DOCX paragraph/table 交错顺序；XLSX 多 sheet、合并单元格、公式、日期、单位和空表头。
- 状态机：`success/partial/no_knowledge/knowledge_base_empty/error` 各分支 schema 校验。
- `rerank_score` 序列化进 `RetrievalResult.to_dict()`。
- `VectorStore` ABC：`InMemoryVectorStore` 与 `MilvusVectorStore` 对 `replace_chunks` 契约的行为一致。

### 集成测试

- 上传 → 解析 → block 模型 → 归并切块 → staging index → 原子发布 → 检索 → 引用回源。
- OCR worker 超时/崩溃后线上 V1 索引保持可用。
- embedding 429/5xx 重试与最终失败回滚。
- parser/chunker/embedding 版本变化自动要求重建。
- 表格行召回后回答引用对应 cell。
- 存储后端切换：`milvus_lite` ↔ `in_memory` 经 ABC 无缝替换。

### 回归集必须包含

- "发票丢了怎么报销"与"遗失发票处理"。
- "出差期间生病的医疗费能报销吗"。
- 北京住宿标准与其他城市标准的范围隔离。
- 明确无答案的问题。
- 同一制度新旧版本冲突。
- 扫描表格中的金额、单位和脚注条件。

## 15. 本次审计的验证记录（2026-08-12 复核）

本轮运行了现有 RAG 相关测试：

```text
pytest -q \
  tests/test_rag_pipeline.py \
  tests/test_rag_production_pipeline.py \
  tests/test_rag_agent.py \
  tests/test_knowledge_base_routes.py \
  tests/test_attachment_processing.py

结果：62 passed, 0 skipped（2026-08-12 实测复核；上一版记录的 51 passed/4 skipped 已过时）
```

额外使用只读/临时目录诊断确认（2026-08-12 复核）：

- 当前 14 个文件（含 4 个 PDF）生成 384 块，248 块不足 100 字符（64.6%），其中 66 块不足 20 字符（仅作孤儿候选，不全等于标题）；4 个 PDF 产生 5 组重复 `chunk_id`。
- Markdown heading 不参与结构切块。
- 连续空格/tab 被普通规范化器压平。
- Pipeline 按 PDF 页切块时 `chunk_index` 每页回到 1。
- 同一文件重复增量入库产生相同 hash 的重复记录。
- 当前 manifest 不包含解析、切块、embedding 或 schema 版本。
- `_tokenize` 已生成 2/3/4-gram 中文 ngram；重排已含 ngram/focus 与"报销"等通用词，但 `filter_relevant_results` 仍在查询含餐费词时才启用且保留 rank-1 免检。
- `.env` 当前 `HOMMEY_EMBEDDING_MODEL=BAAI/bge-m3`、维度 1024（与上一版记录的 bge-large-zh-v1.5 不同）。
- 聊天附件支持图片（VisionClient Qwen2.5-VL），图片识别结果不进入知识库。
- PostgreSQL 后端未启用 pgvector；17+ 个 migration 无任何向量列。

现有测试全部通过只说明当前约定被实现，并不覆盖上述成熟度缺口。新增测试必须先复现这些问题，再开始改造。

## 16. 推荐实施顺序

最终建议顺序如下：

1. **评测和追踪先行**：没有 baseline 就无法判断任何改动是否真的改善。
2. **实现 block 模型 + 泛化切块**（§7）：这是消灭孤儿切块、Markdown 结构丢失、身份不稳定等一揽子问题的总开关，也是后续表格/OCR/引用的共同基础。
3. **修复静默丢页、增量去重和稳定 ID**：正确性问题，与 2 同属 Phase 1。
4. **通用证据门槛 + 状态机统一**（§4.13/§10）：先控制弱证据进入回答。
5. **再接 DOCX、CSV、XLSX 原生结构**：成本低于 OCR，能快速获得表格能力，且验证切块器的泛化。
6. **OCR/layout worker 小流量上线**：对扫描 PDF 和复杂表格按页回退，可复用 VisionClient。
7. **存储仅在触发条件满足时评估**（§8.3）：默认保留 Milvus Lite，不按时间排期做迁移。
8. **通过 Phase Y 后按 Phase 6a～6f 评估 HyDE**：一期不做；二期完成离线验证后作为用户显式选择的增强检索上线，标准检索始终为默认。

一句话原则：先让"真实文档被正确理解和切分"，再考虑"查询产生更多召回路径"。

## 17. 参考资料

- Docling 支持格式：[Supported formats](https://docling-project.github.io/docling/usage/supported_formats/)
- Docling OCR/表格参数：[CLI reference](https://docling-project.github.io/docling/reference/cli/)
- Docling 表格结构序列化：[Serialization](https://docling-project.github.io/docling/concepts/serialization/)
- PaddleOCR PP-StructureV3：[Introduction](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-StructureV3/PP-StructureV3.html)
- PaddleOCR PP-StructureV3：[Usage tutorial](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
- OCRmyPDF 页面处理模式：[Advanced features](https://ocrmypdf.readthedocs.io/en/latest/advanced.html)
- Milvus BM25：[BM25 Function](https://milvus.io/docs/bm25-function.md)
- Milvus dense/sparse hybrid：[Multi-Vector Hybrid Search](https://milvus.io/docs/multi-vector-search.md)
- Milvus Lite 功能范围：[Run Milvus Lite Locally](https://milvus.io/docs/milvus_lite.md)
- Milvus JSON 字段与过滤：[JSON Field](https://milvus.io/docs/use-json-fields.md)
- BGE 中文模型卡与 query instruction：[BAAI/bge-large-zh](https://huggingface.co/BAAI/bge-large-zh)
- pgvector：[pgvector/pgvector](https://github.com/pgvector/pgvector)
- HyDE 原始论文与代码：见附录 A

## 附录 A：HyDE 设计约束（一期不做，作为 Phase 6 实施依据）

> 一期承诺范围**不包含** HyDE（2026-08-12 明确）。一期基础工程完成后，HyDE 只允许按 §11 的 Phase Y 和 Phase 6a～6f 推进；本附录定义 QueryBundle、权重候选、安全规则、prompt 与追踪约束，阶段准入和完成条件以 §11 为准。

HyDE 的原始论文流程是：LLM 生成一段假想相关文档，再将其编码为向量，用该向量在真实语料附近检索。论文同时明确指出假想文档可能包含错误细节。对公司政策尤其是金额、审批和报销资格，这一风险必须隔离。

### A.1 推荐 QueryBundle

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

### A.2 强制安全规则

- HyDE 只生成检索向量，绝不进入 BM25 查询。
- HyDE 文本绝不作为证据、引用或回答上下文。
- 最终回答只能引用真实 chunk。
- HyDE prompt 禁止主动创造金额、日期、城市等级、审批条件；即使如此仍按不可信合成数据处理。
- HyDE 超时、限流或模型错误时无损降级到原始检索。
- trace 中标记 `synthetic=true`，但日志要遵守敏感数据策略。

### A.3 用户显式启用

产品规则：

- 默认使用现有标准检索，不产生 HyDE LLM 调用。
- 用户在输入区选择“增强检索”后，该会话的 RAG 查询使用 HyDE；后端不得基于置信度自动替用户打开。
- 非 RAG 能力不受此选择影响；增强失败无损回退标准，并向前端返回实际模式和失败状态。

适合用户主动选择增强的场景：

- 用户表达模糊、口语化，词面与政策术语差异大。
- 问题描述的是情境或后果，而不是精确条目名称。
- 标准检索没有找到满意答案，希望主动扩大语义召回。

“发票丢了怎么报销”是适合比较标准检索与增强检索的样本。即使用户主动选择增强，HyDE 仍只是检索查询，任何合成内容都不得成为制度证据。

### A.4 HyDE prompt 草案

```text
你只生成用于语义检索的"假想公司制度片段"，不得回答用户。
保留用户问题中的地点、费用类型、日期、例外条件和否定词。
使用公司差旅制度常见术语描述可能相关的条款主题。
不得编造具体金额、比例、时限、审批人或报销结论；未知处使用抽象表述。
输出 80～180 个中文字符，不含解释、引用或指令。
```

### A.5 缓存与追踪

- 缓存键：`normalized_query + model + prompt_version + tenant/policy_scope`。
- 记录每个 query variant、耗时、候选 ID、dense/BM25/RRF/rerank score、过滤原因和最终证据。
- `rerank_score` 需先完成 §9.4 的序列化修复，才能进入追踪。
- 设定最大 query variant 数和总检索预算，避免多查询与 HyDE 乘法膨胀。
