"""Fixed enterprise-travel scope checks; Skills cannot expand tool authority."""

from typing import FrozenSet, Tuple
import re

UNCLEAR_EXACT: FrozenSet[str] = frozenset({
    "你", "你?", "你？", "啊", "啊?", "啊？", "嗯", "嗯?", "嗯？",
    "test", "测试", "随便看看", "看看", "查一下", "帮我查", "查询",
})

FORBIDDEN_ACTIONS: Tuple[str, ...] = (
    "帮我提交审批", "替我提交审批", "帮我提交报销", "替我提交报销",
    "删除服务器", "格式化",
)

BOOKING_KEYWORDS: Tuple[str, ...] = (
    "帮我订", "帮我预订", "帮我预定", "帮我买", "帮我购",
    "预订", "预定", "买票", "购票", "抢票", "订票", "下单",
    "帮我付款", "替我付款", "帮我支付", "替我支付", "代付",
    "直接支付", "帮我转账", "执行转账",
)

OUT_OF_SCOPE_KEYWORDS: Tuple[str, ...] = (
    "写代码", "编程", "python", "java", "javascript", "数据库作业",
    "数学题", "物理题", "化学题", "写作文", "写论文", "股票推荐",
    "娱乐八卦", "星座运势", "情感咨询",
)

PERSONAL_TRAVEL_KEYWORDS: Tuple[str, ...] = (
    "旅游", "度假", "蜜月", "景点攻略", "游玩攻略", "亲子游", "自由行",
)

GIBBERISH_RE = re.compile(r"^[\W_]+$")
