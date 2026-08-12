"""
事项收集智能体
职责：收集用户的出发地/事项地点/事项时间/返程地

核心功能：
- 提取出发地、目的地、时间、返程地等基础信息
- 识别缺失信息并提示
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from core.execution_budget import ExecutionLimitExceeded
from core.trip_intake import evaluate_trip_intake, remove_ungrounded_trip_locations
from typing import Optional, Union, List
import json
import logging
import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

logger = logging.getLogger(__name__)

class EventCollectionAgent(AgentBase):
    """事项收集智能体"""

    def __init__(
        self,
        name: str = "EventCollectionAgent",
        model=None,
        memory_manager=None,
        **kwargs,
    ):
        super().__init__()
        self.name = name
        self.model = model
        self.memory_manager = memory_manager

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content={}, role="assistant")

        # 解析输入内容
        content = x.content if not isinstance(x, list) else x[-1].content

        # 如果content是JSON字符串，解析它
        if isinstance(content, str):
            try:
                data = json.loads(content)
                context = data.get("context", {})
                user_query = context.get("agent_query") or context.get("rewritten_query", "") or str(data)
                user_preferences = context.get("user_preferences", {}) or {}
                active_trip = context.get("active_trip") or {}
                recent_dialogue = context.get("recent_dialogue") or []
            except json.JSONDecodeError:
                user_query = content
                user_preferences = {}
                active_trip = {}
                recent_dialogue = []
        else:
            user_query = str(content)
            user_preferences = {}
            active_trip = {}
            recent_dialogue = []

        # 当前任务和当前会话是已确认事实；偏好/显式引用的历史只能产生待确认候选。
        background_info = ""
        trusted_location_sources = [user_query]
        if active_trip:
            background_info += "【当前出差任务】（在此基础上增量更新）\n"
            background_info += json.dumps(active_trip, ensure_ascii=False, indent=2) + "\n\n"
            trusted_location_sources.extend([
                active_trip.get("origin"),
                active_trip.get("destination"),
            ])
        dialogue_lines = []
        for item in recent_dialogue[-8:]:
            if not isinstance(item, dict) or item.get("role") != "user":
                continue
            text = str(item.get("content") or "").strip()
            if text and text != user_query:
                dialogue_lines.append(f"• {text[:500]}")
                trusted_location_sources.append(text)
        if dialogue_lines:
            background_info += "【当前会话最近提供的行程信息】（仅用于补齐当前任务）\n"
            background_info += "\n".join(dialogue_lines) + "\n\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        prompt = f"""你是企业差旅事项收集专家，负责提取公司出差的基础信息。

【当前时间】
{current_date} {weekday}

{background_info}【用户输入】
{user_query}

【提取要求】
请尽可能提取以下信息：
1. origin - 出发地
2. destination - 目的地
3. start_date - 出发日期（YYYY-MM-DD格式）
4. end_date - 返程日期
5. duration_days - 行程天数
6. return_location - 返程地
7. trip_purpose - 行程目的
8. work_location - 会议、客户或工作地点
9. work_schedule - 已知的会议或工作时间

【日期处理规则】（重要）
- 当前时间是{current_date}
- 用户说"2月27日"或"2.27"等相对时间，请根据当前时间推断完整日期（年月日）
- 用户说"明天"、"后天"、"下周"等相对时间，请根据当前时间计算具体日期
- 所有日期必须输出完整的YYYY-MM-DD格式

【特殊处理】
- 不把公司差旅行程扩展为景点或私人旅游计划
- 用户偏好（包括家庭住址、常去城市）不得补全为当前行程事实；用户没有说出发地时保持缺失
- 当前出差任务已有的字段应保留；用户本轮提供的新信息覆盖旧值
- 可使用最近对话补齐本轮省略的当前行程事实，但不得把旧的、已完成的其他行程混入当前任务
- 最近对话、当前任务和本轮输入有冲突时，以本轮明确表达为准；无法判断时保留当前任务并要求确认

【输出格式】(严格JSON)
{{
    "origin": "北京",
    "destination": "北京",
    "start_date": "2026-02-27",
    "end_date": "2026-02-27",
    "duration_days": 1,
    "return_location": "北京",
    "trip_purpose": "客户会议",
    "work_location": "南京市某客户办公室",
    "work_schedule": "2026-02-27 14:00",
    "missing_info": [],
    "extracted_count": 9,
    "summary": "2月27日前往南京参加客户会议"
}}

缺失的信息在missing_info中列出，对应字段设为null。
"""

        try:
            # 调用模型
            response = await self.model([
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本 - 处理异步生成器
            text = ""
            if hasattr(response, '__aiter__'):
                # 异步生成器，需要迭代获取内容
                async for chunk in response:
                    if isinstance(chunk, str):
                        text = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            text = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
            elif hasattr(response, 'text'):
                text = response.text
            elif hasattr(response, 'content'):
                text = response.content
            elif isinstance(response, dict) and 'content' in response:
                text = response['content']
            else:
                text = str(response) if response else ""

            # 清理文本，移除markdown代码块标记
            text = text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

            # 提取JSON
            start_idx = text.find('{')
            end_idx = text.rfind('}')

            if start_idx != -1 and end_idx != -1:
                json_str = text[start_idx:end_idx+1]
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError as e:
                    # 记录详细错误信息用于调试
                    logger.error(f"JSON parse failed. Text sample: {json_str[:100]}")
                    raise ValueError(f"Failed to parse JSON. Error: {e}")
            else:
                raise ValueError("No JSON found in response")
        except ExecutionLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Event collection failed: {e}")
            result = {
                "missing_info": ["所有信息"],
                "extracted_count": 0,
                "error": str(e)
            }

        # LLM只提取候选事实；来源校验和是否可规划均由确定性规则计算。
        rejected_locations = remove_ungrounded_trip_locations(result, trusted_location_sources)
        if rejected_locations:
            result.pop("summary", None)

        # 模型不得因为本轮只补了一个槽位就遗忘已确认状态。当前会话的 active_trip
        # 是事实源；本轮明确提取出的非空值覆盖它。
        for key in (
            "origin", "destination", "start_date", "end_date", "duration_days",
            "return_location", "trip_purpose", "work_location", "work_schedule",
        ):
            if not result.get(key) and active_trip.get(key):
                result[key] = active_trip[key]

        suggestions = dict(result.get("suggested_fields") or {})
        if not result.get("origin") and user_preferences.get("home_location"):
            suggestions["origin"] = {
                "value": str(user_preferences["home_location"]),
                "source": "preference",
                "reason": "根据你保存的常用出发地",
            }

        # 跨会话历史只有在用户明确说“上次/以前/历史”等指代时才召回，
        # 且只能作为候选，不会直接写入当前行程。
        history_terms = ("上次", "上一次", "以前", "之前", "历史", "去过", "那次行程")
        if any(term in user_query for term in history_terms) and self.memory_manager:
            trips = self.memory_manager.long_term.get_trip_history(limit=1)
            previous_trip = trips[-1] if trips else {}
            for key in ("origin", "destination"):
                if not result.get(key) and key not in suggestions and previous_trip.get(key):
                    suggestions[key] = {
                        "value": str(previous_trip[key]),
                        "source": "history",
                        "reason": "根据你明确提到的上次行程",
                    }
        result["suggested_fields"] = suggestions
        result.update(evaluate_trip_intake(result))

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
