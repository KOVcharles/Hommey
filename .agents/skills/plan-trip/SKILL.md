---
name: plan-trip
description: Deliver a complete company business trip with policy evidence, real transport and nearby hotel choices, a verified meeting place, daily arrangements and unresolved conditions. Use for a trip plan, confirmed trip form, or venue revision; not standalone policy or ticket queries, tourism, or booking.
---

# 规划企业差旅行程

交付目标是一份可执行的企业差旅行程：真实交通与住宿候选、准确会场、适用制度、每日安排及必要待确认项。查到车次和酒店只完成了资料查询，不能据此宣称完整规划完成。

完整差旅、提交行程表单或修改会场时，按需读取 `references/complete-trip.md`。主 Agent 使用 `prepare_trip_options` 进入完整交付分支：运行时查询真实选项，调用 policy_rag，再把制度与选项明确传给 trip_planner，合并输出。无需用户额外说“差旅标准”。此分支会加载完整交付参考给规划角色。单独咨询制度、只查车票等仍走对应任务，不扩展为完整规划。

城市、出发日期、天数和出差目的不足时，整理并保存已知字段，交给现有行程卡片收集；不猜日期或继承其他会话。
首次确认行程时，在行程卡片内让用户从目的地城市的高德结果中选择会议或办公地点。主 Agent 调用 `prepare_trip_options` 时，若缺少已验证地点，运行时会先返回地点确认卡片；用户明确不查询住宿时不以会场阻塞查询。不要反复提取已保存字段，也不用自由文本假装地点已确认。
地点确认后查询真实车次，并把会场与按偏好排序的附近酒店放在同一张地图和结果卡片内。卡片修改会场时提交新的 POI ID，服务端重新验证城市并更新行程，再按新坐标重新查询，生成新的结果卡片；不能把旧酒店挂到新会场上。地图加载失败保留地址与酒店信息。
单独咨询制度或记忆时使用对应任务，不额外查询车次酒店。明确排除的查询继续排除。偏好不等于报销资格，未知差标不阻止展示真实候选，也不能宣称整体合规。

按需读取：地点歧义、切换城市或确认地点时，使用 `read_skill(name="plan-trip", resource="references/place-selection.md")`；筛选车次酒店或解释失败时，使用 `read_skill(name="plan-trip", resource="references/travel-choices.md")`。不要默认加载所有参考文件。

以下日程编排由 trip_planner 角色完成，只使用明确传入的行程与专业结果，不调用主 Agent 专用工具。
核对工作日程、已知制度、交通、天气、酒店候选和用户排除项，再给出安排。
缺少输入时报告 missing_info，由主 Agent 决定查询或补问；不能自行调度其他 Agent。
以工作安排和可靠到达为先，保留通勤与换乘缓冲。酒店参考消费不等于可预订房价。
车次、余票、金额必须来自输入资料；没有真实车次就不编造车次编号。
data.itinerary.days 每天包含 date 和 activities 文本数组，data.decision_basis 说明选择依据。
方案生成不代表真实出行完成，不自行断言整体合规，不执行任何交易。
