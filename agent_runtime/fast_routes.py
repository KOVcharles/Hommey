"""Full-input routes only: never consume just one clause of a compound request."""
import re


def weather_policy_tasks(text):
    # Intentionally narrow. Negation, conditions, extra clauses and attachments
    # must stay with the supervisor, whose input retains the complete request.
    match = re.fullmatch(
        r"\s*(?:请)?(?:帮我|给我)?(?:查一下|查询|看看|查查)"
        r"(?P<city>[\u4e00-\u9fff]{2,8}?)(?:的)?天气"
        r"\s*(?:以及|还有|和|及|并查询|并查一下)\s*(?:相关的?|当地的?)?"
        r"(?:差旅|出差)(?:标准|制度)[。.!！?？\s]*", text)
    if not match or any(word in match["city"] for word in (
        "不", "别", "如果", "明天", "今天", "后天", "和", "与", "及", "或", "还是", "再", "同时", "顺便", "下周", "本周", "周末")):
        return None
    city = match["city"]
    return [
        {"role": "travel_info", "task": f"查询{city}天气；仅交付天气及预报适用日期，不查询交通或酒店。",
         "step_id": "result_route_weather", "title": f"查询{city}天气", "purpose": "确认当地天气与预报适用日期"},
        {"role": "policy_rag", "task": f"查询适用于{city}的企业差旅标准；标明适用条件和未知项。",
         "step_id": "result_route_policy", "title": f"查询{city}差旅标准", "purpose": "确认当地适用的交通、住宿和报销要求"},
    ]
