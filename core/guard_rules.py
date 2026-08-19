"""声明式 guard 关键词规则。

关键词表作为**数据**存放在这里（而不是散落在各函数体里），这样新增一个
skill-backed 意图时，guard 的扩展可以是纯声明式的。

guard 只做"可证明无歧义"的安全网（LLM 主导识别）：垃圾/寒暄/明确领域外
请求/高风险操作在此短路，其余一律放行给 LLM。交易类关键词（订票/付款/转账）
**不**构成黑名单 —— 订票请求放行给 LLM，由产品边界 prompt 判定为 unsupported；
目录中一旦新增 ``TRANSACTION_INTENTS`` 中的意图（例如未来的 ``ticket_purchase``），
同一批关键词就自动成为可被识别的意图（纯声明式扩展，零 Python 改动）。

消费方：core/intent_guard.py（规则逻辑）、core/intent_router.py（候选检测）。
"""
from __future__ import annotations

from typing import FrozenSet, Tuple

import re

from core.intent_catalog import is_skill_intent

# 短输入精确匹配集：必须在 guard 的 length<=2 判断之前命中，
# 否则"你好/在吗/嗨"等 1~2 字输入会被误判为 unclear。
UNCLEAR_EXACT: FrozenSet[str] = frozenset({
    "你", "你?", "你？", "啊", "啊?", "啊？", "嗯", "嗯?", "嗯？",
    "test", "测试", "随便看看", "看看", "查一下", "帮我查", "查询",
})

# 高风险操作：代替用户提交审批/报销、破坏性操作。无论 skill 目录如何都拒绝。
FORBIDDEN_ACTIONS: Tuple[str, ...] = (
    "帮我提交审批", "替我提交审批", "帮我提交报销", "替我提交报销",
    "删除服务器", "格式化",
)

# 交易/订票语言。guard 不再用它们硬拒 —— 订票请求放行给 LLM 判定；当目录中
# 出现 TRANSACTION_INTENTS 对应的 skill 意图时，这些词成为可识别意图。
TRANSACTION_KEYWORDS: Tuple[str, ...] = (
    "订票付款", "帮我订票", "直接预订", "帮我预订", "帮我付款", "替我付款", "代付",
    "直接支付", "帮我支付", "替我支付", "帮我转账", "执行转账",
)

# 预订/购票/付款等交易语言（黑名单）。guard 在无 ticket_purchase/payment skill
# （transaction_supported()=False）时确定性拒绝 —— 产品边界「仅建议、不交易」；
# 目录出现 TRANSACTION_INTENTS 对应 skill 后 transaction_supported() 变 True，
# 同一批词自动放行（纯声明式扩展，零 Python 改动）。
BOOKING_KEYWORDS: Tuple[str, ...] = (
    "帮我订", "帮我预订", "帮我预定", "帮我买", "帮我购",
    "预订", "预定", "买票", "购票", "抢票", "订票", "下单",
    "帮我付款", "替我付款", "帮我支付", "替我支付", "代付",
    "直接支付", "帮我转账", "执行转账",
)

# 消费交易语言、可被放行的意图名。新增车票 skill 时把其 intent 加进该集合，
# 或者直接复用其中一个名称 —— 一旦目录中出现，transaction_supported() 变 True。
# （纯声明式扩展探测点：tests/test_skill_catalog_derived.py 验证闭环。）
TRANSACTION_INTENTS: FrozenSet[str] = frozenset({
    "ticket_purchase", "ticket_booking", "payment",
})

OUT_OF_SCOPE_KEYWORDS: Tuple[str, ...] = (
    "写代码", "编程", "python", "java", "javascript", "数据库作业",
    "数学题", "物理题", "化学题", "写作文", "写论文", "股票推荐",
    "娱乐八卦", "星座运势", "情感咨询",
)

PERSONAL_TRAVEL_KEYWORDS: Tuple[str, ...] = (
    "旅游", "度假", "蜜月", "景点攻略", "游玩攻略", "亲子游", "自由行",
)

BUSINESS_TRAVEL_KEYWORDS: Tuple[str, ...] = (
    "出差", "差旅", "商旅", "商务行程", "公务出行", "拜访客户", "客户拜访",
    "会议地点", "会场", "差旅任务", "出差任务",
    "报销", "发票", "补贴", "餐补", "住宿标准", "交通标准", "差旅标准",
    "差旅政策", "差旅制度", "审批", "超标", "改签", "退票", "延误",
    "合规", "符合标准", "检查行程", "行程检查",
    "行程规划", "规划行程", "安排行程", "出行方案", "路线怎么走", "怎么走最好",
    "我去过", "差旅记录", "出差记录", "喜欢住", "常住酒店", "常坐", "靠窗座位",
)

TRAVEL_TRANSPORT_KEYWORDS: Tuple[str, ...] = (
    "航班", "机票", "机场", "高铁", "火车", "车次", "动车", "铁路",
    "车票", "火车票", "高铁票", "动车票",
    "酒店", "住宿", "地铁", "打车", "交通路线", "出行路线", "换乘",
)

# ---- FastIntentRouter 检测关键词（数据化，供 intent_router.py 候选检测使用）----
POLICY_KEYWORDS: Tuple[str, ...] = (
    "报销", "差旅政策", "住宿标准", "补贴",
    "餐补", "餐费", "餐饮", "饭补", "补助", "津贴",
    "住宿费", "交通费", "差旅费", "发票",
)
GENERIC_POLICY_KEYWORDS: Tuple[str, ...] = ("标准", "流程")
WEATHER_KEYWORDS: Tuple[str, ...] = ("天气", "气温", "下雨", "预报")
# 车次/高铁/火车/铁路 → train_query（与信息查询划界；TRAVEL_TRANSPORT_KEYWORDS
# 保留原样供 has_business_travel_context 识别，二者职责不同）。
TRAIN_KEYWORDS: Tuple[str, ...] = (
    "车次", "高铁", "火车", "动车", "铁路", "班次", "时刻表", "坐高铁", "坐火车",
    "车票", "火车票", "高铁票", "动车票",
)
SEARCH_KEYWORDS: Tuple[str, ...] = ("查一下", "搜索", "查询", "了解一下")
COMPLIANCE_KEYWORDS: Tuple[str, ...] = ("合规", "符合标准", "检查行程", "行程检查", "是否超标")
MEMORY_KEYWORDS: Tuple[str, ...] = (
    "我去过", "我以前去过", "我之前去过", "我曾经去过", "我住过", "我坐过",
    "我的差旅", "差旅记录", "出差记录", "上次出差", "过去行程", "我的出行偏好"
)
PREFERENCE_KEYWORDS: Tuple[str, ...] = (
    "我喜欢", "我常坐", "我常住", "我住在", "我家在", "我偏好", "我习惯", "我不喜欢"
)
TRIP_KEYWORDS: Tuple[str, ...] = (
    "帮我规划", "帮我安排", "规划行程", "安排行程", "规划路线", "出行方案",
    "怎么走最好", "路线怎么走",
)

# 可脱离额外上下文直接判定为差旅制度的问题。泛化的“报销/发票/补贴/流程”
# 不在这里，否则医疗报销、采购审批、政府补贴等领域会绕过 LLM 消歧。
TRAVEL_SPECIFIC_POLICY_KEYWORDS: Tuple[str, ...] = (
    "差旅政策", "差旅制度", "差旅标准", "住宿标准", "交通标准",
    "差旅费", "住宿费", "餐补", "饭补",
)

AMBIGUOUS_POLICY_KEYWORDS: Tuple[str, ...] = (
    "报销", "发票", "补贴", "补助", "津贴", "餐费", "餐饮", "交通费", "流程", "标准",
)

# 用户本轮明确指定这些非差旅领域时，不能被历史中的出差上下文“带回”差旅
# RAG。该集合只放可证明冲突的领域词，模糊追问仍可继承当前行程上下文。
NON_TRAVEL_POLICY_KEYWORDS: Tuple[str, ...] = (
    "采购", "医疗", "医保", "医药", "学费", "社保", "年假", "请假",
    "办公用品", "政府补贴",
)

GIBBERISH_RE = re.compile(r"^[\W_]+$")


def transaction_supported() -> bool:
    """目录中是否存在消费交易语言的 skill 意图。

    返回 False 时 guard 对订票/付款类请求确定性拒绝；加入对应 skill 后
    返回 True，同一批关键词自动放行 —— 纯声明式扩展，零 Python 改动。
    """
    return any(is_skill_intent(intent) for intent in TRANSACTION_INTENTS)
