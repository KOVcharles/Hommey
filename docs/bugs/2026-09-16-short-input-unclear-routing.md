# 短输入「1」被误当作差旅任务 Bug 记录

> 编号：BUG-2026-09-16-01<br>
> 发现日期：2026-09-15<br>
> 记录日期：2026-09-16<br>
> Bug 状态：本地代码已修复，相关回归已通过，未部署<br>
> 解决方案状态：已实施输入状态绑定、澄清出口、失败收敛和展示校验<br>
> 性质：不只是闸门漏判，暴露了 System Prompt 与工具契约的工程设计问题

---

> **2026-09-16 实施更新：** 下文保留发现时的分析与代码行号，部分方案经评估后调整。实际实现没有按状态名排除所有 `needs_input`，也没有把所有错误一律终止；改为明确的待答问题绑定、合法提交依据和分类恢复策略。详见 [修复说明与回归记录](../changelog/2026-09-16-dialogue-routing-contract.md)。

## 一、用户现象

用户在企业差旅会话中发送了一个内容为 `1` 的输入。

期望：这是一句无法对应任何差旅任务的输入，应当立即得到一句澄清引导，或至少
零成本地被拒绝。

实际：系统把它当作一个真实差旅任务进入了 supervisor 主流程，读取差旅技能、
委派行程收集、尝试保存、反复重试，最终从兜底分支里掉出一张**标题为「行程框架
已保存」但实际未保存任何内容**的行程收集卡片。

### 观察到的时间与调用成本

| 请求时间（北京时间） | 耗时 | 模型调用 |
| --- | --- | --- |
| 21:43:09 的「1」 | 12.3 秒 | 主 Agent 5 次 + 子 Agent 2 次 |
| 21:43:45 的「1」 | 49.8 秒 | 主 Agent 10 次 + 子 Agent 4 次 |

第一条收到后约 **56 毫秒**即开始调用模型，说明耗时几乎全部发生在下游处理，而不是
排队或等待。

### 第二轮的实际链路

1. 主 Agent 读取 `plan-trip` 技能及完整规划说明（额外读取了地点、车次酒店 references）。
2. 委派 `trip_context`，委派任务文本包含「用户输入'1'……如果没有任何行程，请新建一个空行程」。
3. 子 Agent 提交空行程，被校验拒绝，随后再次调用模型修改结果。
4. 主 Agent 尝试保存，被拒绝：`EMPTY_CHANGESET`，没有可保存的变更。
5. 其中一次模型请求等待约 **30 秒**后返回 HTTP 400（现有日志未记录该 400 的错误正文）。
6. 主 Agent 重新委派了一次任务。
7. 最终 `finish` 进入兜底分支，生成行程收集卡片。

两轮均**没有成功保存任何有效行程变更**。

### 卡片为什么会显示「北京」和「行程框架已保存」

「北京」来自用户档案中的常驻城市，被展示层预填为出发地（见 §3.3）。
「行程框架已保存」是一个**不实标题**——它与是否真正保存过无关（见 §3.3）。

---

## 二、执行证据（代码定位）

| 位置 | 内容 |
| --- | --- |
| `agent_runtime/engine.py:176` | `guard_user_input(self.user_text, conversation_context="当前企业差旅会话")` |
| `agent_runtime/engine.py:922` | `guard_user_input(request.task, conversation_context="当前企业差旅委派任务")` |
| `core/intent_guard.py:63` | `if length <= 2 and not conversation_context:` —— 短输入安全网 |
| `core/guard_rules.py:6-9` | `UNCLEAR_EXACT` 精确清单，不含 `"1"` |
| `core/guard_rules.py:33` | `GIBBERISH_RE = ^[\W_]+$`，数字属于 `\w`，不匹配 |
| `agent_runtime/engine.py:177-184` | guard 判决分支，只有 `unsupported` / `chitchat`，**无 `unclear`** |
| `agent_runtime/engine.py:889-892` | `finish` 兜底：trip 未就绪 + 结果均为 `trip_context` → 直接出行程卡片 |
| `agent_runtime/engine.py:899-901` | 同上模式第二处 |
| `core/presentation/trip_intake_document.py:175` | 卡片标题判定 |
| `core/presentation/trip_intake_document.py:127-130` | 用 `home_location` 预填 `origin` |
| `agent_runtime/engine.py:578-580` | 「有进展」判定 |
| `agent_runtime/engine.py:498-501` | 原地打转刹车 |
| `agent_runtime/contracts.py:29` | `Finish.kind: Literal["answer", "ask", "refuse"]` |
| `agent_runtime/model_client.py:78` | `max_retries=0` |

---

## 三、根因

输入 `1` 一路穿过**四道本应拦截它的闸门**，每道失败的原因都不同。

### 3.1 guard 的短输入安全网被一个硬编码常量永久关闭

`core/intent_guard.py:63` 的短输入保险写法是：

```python
if length <= 2 and not conversation_context:
    return _unclear("输入太短，无法判断具体意图")
```

设计意图见 `tests/test_intent_guard.py:19-27`：**只有当短输入确实可能是「补充字段值」
（如出差目的「培训」、出发地「北京」）时才传入上下文**，从而在这类场景放过短输入。

但调用点 `engine.py:176` 传入的不是真实上下文，而是一句**写死的字符串**
`"当前企业差旅会话"`。该字符串永远非空，因此**这个安全网对所有轮次永久失效**，
而不只是 intake 收集期间。

对 `"1"` 而言，guard 的完整判定链全部不匹配：

| 判定 | 结果 |
| --- | --- |
| 空输入 | 否 |
| `CHITCHAT_EXACT` | 否 |
| `UNCLEAR_EXACT`（"你" / "啊" / "嗯" / "看看" / "查一下" …） | **不在清单内** |
| `GIBBERISH_RE`（`^[\W_]+$`） | 否——`\d` 属于 `\w` |
| 高危操作 / 订票 / 超范围 / 私人旅游 | 否 |
| **`length <= 2 and not conversation_context`** | **被跳过** |

最终返回 `None`，语义是「**我判断不了，交给大模型**」。

**guard 从未说过 `unclear`。**

### 3.2 主流程没有 `unclear` 这扇门

`agent_runtime/engine.py:177-184` 只处理了两种判决：

```python
if guard and guard.intent == "unsupported":
    output = render(Finish(kind="refuse"), [])
elif guard and guard.intent == "chitchat":
    output = {...}
elif ... is_intake_entry(...) ...:
    ...
else:
    await self.emit("analyzing")   # 走完整 supervisor 流程
```

**没有任何分支处理 `unclear`**，因此任何非上述判决都会落入 `else`。

需要强调的是：**即使修好 §3.1，只补这个分支也不够**——因为对 `1` 来说 guard 返回的
是 `None` 而不是 `unclear`。两个缺陷必须一起修，且 §3.1 是前提。

> **附带发现：`GuardResult.clarification` 是死代码。**
> `core/intent_guard.py:90` 与 `:110` 为 `unclear` 和 `unsupported` 都写好了中文澄清
> 文案，但全项目**没有任何一处读取它**。即使走 `unsupported` 分支，也只返回
> `render(Finish(kind="refuse"), [])` → `REFUSAL` 常量（`render.py:13-14`）。
> guard 里那段精心写的引导语永远不会出现在用户屏幕上。

### 3.3 卡片标题不实：与「是否保存过」无关（独立缺陷）

「行程框架已保存」不是 `EMPTY_CHANGESET` 的副产物，而是展示层的独立缺陷：

`core/presentation/trip_intake_document.py:175`
```python
title = "补充出差信息" if not collected else "行程框架已保存"
```

而 `collected` 之所以非空，是因为 `trip_intake_document.py:127-130` 用档案中的
`home_location` 预填了 `origin` 并标记 `source="memory"`：

```python
if home and not str(raw_data.get("origin") or "").strip():
    data = {**raw_data, "origin": home, "origin_inferred": True}
```

结论：**只要用户档案里有常驻城市，标题就必然显示「行程框架已保存」**，
与是否真正 apply 过任何变更无关。即使一次 `EMPTY_CHANGESET` 都没发生，该标题也是错的。

「北京」与「已保存」这两个误导性观感的真正来源都在这里，不在 §3.4。

### 3.4 空结果仍会触发行程卡片

`agent_runtime/engine.py:889-892` 的判定条件：

```python
if request.kind != "refuse" and not evaluate_trip_intake(self.state["trip"])["planning_ready"]:
    candidates = selected or list(self.results(list(self.state["results"])))
    if candidates and all(r.role == "trip_context" and r.data.get("trip_action") != "cancel" for r in candidates):
        return {"_terminal": {**intake_output(...), "outcome": "waiting_input"}}
```

谓词**只检查了 `r.role` 和 `trip_action`，完全没有检查 `r.status`**。而
`SpecialistResult.status`（`contracts.py:49`）允许 `error` / `unavailable` / `needs_input`。
因此一个**失败或为空**的 `trip_context` 结果同样会生成行程卡片。`:899-901` 是同一模式。

### 3.5 「空行程」不是功能，是误读的下游产物

系统里**不存在**「建空行程」这个功能或角色。`trip_context`（`profiles.py:14`）是
**翻译型**角色——把用户原话转成结构化字段，没有任何查询工具：

```python
"trip_context": Profile("行程信息整理", "...", (), ("event-collection",))
#                                             ↑ tools 为空
```

因此 `engine.py:490-494` 给它套了特殊通道：只剩 `report` 工具，最多 2 轮预算。
**它在一个没有任何外部信息可查的角色上，把 2 轮预算全部用于空转。**

空行程是主 Agent 把 `1` 误读为出差意图后，在**委派任务文本**里写下
「如果没有任何行程，请新建一个空行程」，子 Agent 只是照做。

值得注意的是：系统随后**正确地拒绝了它两次**——原文门工作良好：

- `engine.py:738-743`：`ToolRejected("不能把空行程标记成功…", code="EMPTY_TRIP_REPORT")`
- `validation.py:58`：`ToolRejected("该结果没有可提交的行程或偏好变更", code="EMPTY_CHANGESET")`

**问题不在于它拒绝错了，而在于拒绝之后的处理方式**——见 §3.6。

### 3.6 拒绝被当作「重试信号」而不是「终止信号」

工具被拒绝时，错误文本被塞回对话（`engine.py` 的 `tool_message` 写入），
模型看到后重新决策。**每一次重新决策就是一次全新的模型调用，并消耗一轮预算。**

于是这个**本应是终点**的信号被当成了**又一次尝试的起点**：

```
委派子 Agent 建空行程 → 被校验拒绝 → 模型再想一次（+1 调用）
主 Agent 尝试保存     → EMPTY_CHANGESET → 模型再想一次（+1 调用）
准备追问             → 被兜底分支拦截  → 模型再想一次（+1 调用）
```

**在这套设计里，「失败」不省调用，反而更费调用。**

### 3.7 检查并修了刹车失灵（成本放大的直接原因）

代码里有防死循环机制（`engine.py:498-501`）：连续 3 轮无进展则强制收敛或 fallback。

但「什么算进展」的判定是（`engine.py:578-580`）：

```python
progressed = any(not v.get("error") and not v.get("reused")
                 and c["name"] not in {"read_result", "discard_result"}
                 for c, v in zip(calls, values))
```

只要这轮有一个工具**没报错、且不是复用**，计数器即清零。

问题在于：**「委派子 Agent 成功、但子 Agent 交回空结果」——没报错，也不是复用，
所以被算作「有进展」**。子 Agent 每交一次空结果，就把刹车计数器清零一次。

**刹车永远踩不下去**，于是一路烧到 10 轮。

### 3.8 关于预算：10 次不是故障，是默认行为

`settings.py:217-218`：

```python
"main_rounds":  max(2, min(_int_env("HOMMEY_SUPERVISOR_MAIN_ROUNDS", 14), 24)),   # 默认 14
"child_rounds": max(2, min(_int_env("HOMMEY_SUPERVISOR_CHILD_ROUNDS", 6), 10)),   # 默认 6
```

预算按「轮」给，**每一轮＝一次模型调用**。主 Agent 走到 10 次时**还剩 4 次没用完**。
第一轮 5 次只是它更早凑到了能交差的结果。

此外 `model_client.py:78` 的 `max_retries=0` 意味着上游 400 **不重试**，直接判失败，
主 Agent 随后选择「再委派一次」——又是一轮。这解释了第二轮为何被拉到约 50 秒。

---

## 四、Prompt 设计问题（重点）

前几节都是代码缺陷。但即便全部修好，**同类问题仍会以新的形式复发**，因为主 Agent
的 System Prompt 本身存在工程性问题。

本节是本文档的核心：**我们要改的不是再加一条规则，而是 prompt 的组织方式。**

### 4.1 现状：它不是一份规格，是一份「历史补丁的堆积层」

`MAIN_RULES`（`agent_runtime/profiles.py:49-70`）共 21 条。逐条对照代码后，几乎每条
都能反推出它对应哪一次线上 bug：

| 规则原文 | 显然是因为曾经 |
| --- | --- |
| `51` 无需用户额外说差旅标准 | 追问过差旅标准 |
| `52` 不重复委派 trip_context | 重复委派过 |
| `59` 先 discard_result 再委派修正 | 拿到坏结果直接用了 |
| `62` 不要要求子 Agent 返回完整条款 | 要求返回过全文 |
| `63` 不重新委派追求穷尽 | 反复委派过 |
| `69` 不要自己重新编写原始车次 | 自己编过车次 |
| `70` 只返回原生工具调用 | 用文本模拟过工具调用 |

这些全是**症状**，不是**原则**。每修一个 bug 加一句，prompt 就退化成一份变更日志。

**如果按同样方式修本 bug，加的就是第 22 条——然后下一个类似输入再加第 23 条。**
这与「写 if-else」没有本质区别，只是把分支从代码搬到了自然语言里，而且
**自然语言分支无法被测试、无法被静态检查、无法保证互不冲突**。

### 4.2 规格冲突：规则之间没有优先级

对输入 `1`，至少三条规则同时适用，且**互相矛盾**：

| 规则 | 指向 |
| --- | --- |
| `66` 拒绝一切非企业差旅请求 | 该拒绝 |
| `57` 新行程/修订**先委派** trip_context | 该委派 |
| `64` trip_context 缺信息**也要**选进 finish，系统生成表单 | 该出表单 |

prompt **没有定义谁优先**。模型挑了 `57 + 64` 的组合。

**这不是模型判断失误，这是规格本身的缺陷**——一份同时说「做 A」「做 B」「做非 A」
且不声明优先级的规格，执行结果是随机的。

### 4.3 没有「意图不明」这一类，也没有兜底规范

通读 21 条，关于「什么时候该问用户」**只有一条**：

> `65` 需要资料时补查或 `finish(kind=ask)`。

注意措辞——**「需要资料」**。这是「缺事实」，不是「看不懂你想干嘛」。
全篇**没有任何一条**定义「输入无法对应任何差旅任务时怎么办」。

**任何规格系统都必须定义「当我不匹配任何规则时怎么办」。** 这份 prompt 的隐含兜底
是「做点差旅的事」——这正是 §3.5 空行程的来源。

### 4.4 注意力预算错配：一半 token 花在讲内部 API

`52`（prepare_trip_options 是受控入口）、`56`（result_ids 传依赖）、
`59`（discard_result）、`68`（finish 的 result_ids）……这些是**运行时机制**，
本应由工具 schema 表达，却占据了 system prompt 一大半的注意力预算。

而真正需要判断力的部分——「**这句话到底是什么意思**」——21 条里几乎没有着墨。

**把 token 花在模型不需要「理解」只需要「知道」的事情上，是对上下文的浪费。**

### 4.5 根本病因：工具契约贫瘠 → prompt 膨胀

这是最根本的一条，也是 §4.4 的成因。

`contracts.py:29`：
```python
class Finish(StrictModel):
    kind: Literal["answer", "ask", "refuse"] = "answer"
```

只有三种。语义分别是：

- `refuse` =「**这不是差旅请求**，我拒绝」——对应规则 66
- `ask` =「**缺信息**，我问用户」——对应规则 65
- `answer` = 正常交付

**没有「意图不明」这个选项。** 模型即使完全想通了「我看不懂 1 是什么意思」，
也只能硬塞进 `ask`（语义是缺信息）或 `refuse`（语义是非差旅请求）。

而它两个都不好用：`refuse` 需要判定「1 是一个非差旅**请求**」，可 `1` 里没有任何
内容可供判定；`ask` 需要提出「缺哪个字段」的问题，可 `1` 缺的不是字段。

**运行时没有给模型表达「我不确定」的能力，于是我们只能在 prompt 里用自然语言
拼命描述各种场景去补偿。散文不能强制任何东西，所以只能写更多散文。**

> **契约贫瘠与 prompt 膨胀是同一个病的两面。**

### 4.6 工程性最差的一点：不可测试

21 条自然语言规则，**没有一条可以被单独验证**。

`tests/test_intent_guard.py` 能测 guard，是因为 guard 是代码。prompt 的行为只能靠
「跑一遍看看」。因此每次改 prompt 都是**不可回归的改动**：你不知道修好没有，
也不知道弄坏别的没有。

**这直接解释了本 bug 为何能存活**——现有测试覆盖的是 guard 函数，没有任何测试
覆盖 prompt 的决策行为。

### 4.7 一个补充反思：guard 用错了地方

需要澄清一个误区：**问题不是「硬规则不好」，而是「硬规则被用在了需要语义的地方」。**

当前 `guard` 在用**关键词列表做语义判断**——这是最坏的一种组合：

- 它想像模型一样理解语义（所以维护 `UNCLEAR_EXACT` 列表、配语气词）
- 又想享受代码的确定性（所以硬编码）

结果两头不占：**该由状态判断的事，用了语义猜测。**

「`1` 能否被消费」**本质上不是语义问题，是状态问题**——系统知道上一轮给了什么
编号选项、当前处于哪个流程阶段、行程是否在收集中。这个判断不需要理解自然语言，
也不应该写死关键词，它需要的是**流程状态机**。

正确的切分应当是：

| 判断类型 | 归属 |
| --- | --- |
| 这一轮输入能否被当前流程消费 | **运行时状态机**（确定性、零成本） |
| 用户到底想要什么 | **模型**（配好契约与规格） |

现在的 `guard` 卡在中间，两边都不像。

---

## 五、修复方向

### 5.1 代码层

| # | 位置 | 改动 |
| --- | --- | --- |
| 1 | `engine.py:176` / `:922` | 去掉硬编码常量，按真实流程状态决定是否传上下文（**修完此项，`1` 是 0 次调用**） |
| 2 | `engine.py:177-184` | 补 `unclear` 分支，并消费 `GuardResult.clarification`（同时修 §3.2 死代码） |
| 3 | `engine.py:889-892` / `:899-901` | 谓词补 `r.status in {"success", "partial"}` 与载荷非空判断 |
| 4 | `trip_intake_document.py:175` | 标题依据实际 apply 结果，而非 `collected` 非空 |
| 5 | `engine.py:578-580` | 「有进展」判定改为 `v.get("status") in {"success","partial"}`（此项影响**所有真实任务**的成本） |
| 6 | 拒绝处理路径 | 连续拒绝视为终止信号，而非重试信号 |

> 注：`d5f1825 fix: lock per conversation so one window no longer blocks another`
> 位于 `codex/session-concurrency` 分支，**不是当前 HEAD 的祖先**。当前运行代码仍
> 使用 `webui_new/manager.py:649-654` 的 per-user 锁。此项与本 bug 独立，但会
> 影响回归验证时的并发行为。

### 5.2 Prompt 与契约层（本次重点）

**原则：先定契约，再写 prompt。**

1. **补全工具契约。** `Finish.kind` 至少增加 `clarify`（意图不明）。schema 定好后，
   prompt 中大量场景描述可以被枚举值取代——**契约每补一项，prompt 就能删一段。**

2. **按「阶段」重组，而非按「症状」堆叠。** 建议三段结构：
   - 判断：这是什么任务（含「无法归类」的明确出口）
   - 执行：怎么做
   - 兜底：不认识时怎么办（**必须显式定义**）

3. **删除代码可强制的规则。** `70`（只返回原生工具调用）应由解析层强制；
   `59`（坏结果先 discard）应是运行时状态。**prompt 只保留代码表达不了的判断。**
   每删一条能强制的规则，就多一分注意力预算留给真正需要判断的地方。

4. **声明规则优先级。** 存在冲突的规则必须显式标出优先关系，否则执行结果是随机的。

### 5.3 建议的 prompt 验收方式

**建立 prompt 回归用例集**：一组 `输入 → 期望行为` 的固定用例，让 prompt 的改动
能像代码一样被验证。至少覆盖：

| 输入 | 期望行为 |
| --- | --- |
| `1` | `clarify`，**零委派、零保存** |
| `帮我写一个 Python 程序` | `refuse` |
| `去南京出差` | 委派 `trip_context` |
| `随便看看` | `clarify` |
| `公司差旅住宿标准` | 委派 `policy_rag` |

**这是让 prompt 改动可回归的唯一办法**，也是 §4.6 的正面解法。

---

## 六、尚未消除的风险

1. **同类输入仍有其他形态。** 本记录只覆盖了 `1`。`2`、`3`、`好的`、`嗯嗯` 等
   短输入是否走同一路径，需按 §5.3 的用例集扩展验证。

2. **§4.5 的契约贫瘠问题若不解决，prompt 的膨胀趋势不会逆转。** 本次即使按
   §5.2 重写 prompt，只要 `Finish.kind` 仍是三个值，下一类「模型无法表达的状态」
   仍会以自然语言补丁的形式回到 prompt 里。

3. **§3.7 的刹车判定影响所有任务成本，但改动需谨慎。** 收紧「有进展」的判定可能
   让部分合法的多轮任务提前触发 `NO_PROGRESS` fallback，需要回归验证。

4. **HTTP 400 的响应正文未落日志**，无法定位上游失败原因。建议在
   `model_client.py` 的异常路径中记录响应正文（注意脱敏）。

5. **跨分支状态。** 并发修复（`d5f1825`）尚未合入，本 bug 的回归验证若在合并后
   进行，需重新确认基线行为。

---

## 七、验收标准

修复后应满足：

- [x] 无待答问题时输入 `1`：**0 次模型调用**，返回澄清引导，不产生任何委派、保存或行程卡片。
- [x] 同会话上一轮明确等待天数或编号选择，且行程版本未变时，`1` 可被对应问题消费；仅有进行中的行程不足以授权。
- [x] `EMPTY_CHANGESET` 直接转向补问；`EMPTY_TRIP_REPORT` 用完有限修复次数后停止并澄清，不重复委派。
- [x] 空结果或失败结果**不会**生成行程收集卡片；明确要求开始出差仍可显示未保存的空表单。
- [x] 没有行程保存依据时，卡片标题**不出现**「已保存」字样。
- [x] §5.3 的行为验收场景及额外模糊表达完成离线/真实模型回归；具体覆盖和调用上限见修复说明。
