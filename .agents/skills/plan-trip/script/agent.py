"""
行程规划智能体
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from core.execution_budget import ExecutionLimitExceeded
from typing import Optional, Union, List, Dict, Any
import json
import logging
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))
# project_root = Path(__file__).parent.parent # Removed old logic
# sys.path.insert(0, str(project_root))

from utils.json_parser import robust_json_parse, extract_json_from_async_response

logger = logging.getLogger(__name__)


def normalize_planning_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize common model output variants to the public itinerary contract."""
    if not isinstance(result, dict) or isinstance(result.get("itinerary"), dict):
        return result

    trip = result.get("trip")
    if not isinstance(trip, dict):
        return result

    transport = trip.get("transport_recommendation") or {}
    if isinstance(transport, dict):
        transport = {
            "preferred": transport.get("mode") or transport.get("preferred"),
            "reason": transport.get("advice") or transport.get("reason"),
            "verification": transport.get("note") or transport.get("verification"),
        }

    lodging = trip.get("lodging_advice") or ""
    if isinstance(lodging, dict):
        lodging = "\n".join(
            str(value) for value in (
                lodging.get("hotel"),
                lodging.get("location_advice"),
                lodging.get("note"),
            ) if value
        )

    daily_plans = []
    for index, day in enumerate(trip.get("daily_schedule") or [], start=1):
        if not isinstance(day, dict):
            continue
        activities = []
        for activity in day.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            activities.append({
                "time": activity.get("time", ""),
                "activity": activity.get("activity") or activity.get("description", ""),
                "description": activity.get("details", ""),
                "transport": activity.get("transport", ""),
            })
        daily_plans.append({
            "day": index,
            "date": day.get("date", ""),
            "activities": activities,
        })

    notes = []
    if trip.get("weather_advice"):
        notes.append(trip["weather_advice"])
    compliance_note = (trip.get("compliance_check") or {}).get("note")
    if compliance_note:
        notes.append(compliance_note)

    duration = trip.get("duration_days") or ""
    result["itinerary"] = {
        "title": f"{trip.get('origin', '')}至{trip.get('destination', '')}出差行程".strip("至"),
        "duration": f"{duration}天" if duration else "待确认",
        "transport_recommendation": transport,
        "lodging_advice": lodging,
        "daily_plans": daily_plans,
        "notes": notes,
        "reimbursement_checklist": trip.get("reimbursement_checklist") or [],
        "missing_info": trip.get("missing_info") or [],
    }
    result["planning_complete"] = True
    return result


class ItineraryPlanningAgent(AgentBase):
    """
    行程规划智能体（主协调）
    职责：协调事项收集、路线规划、酒店规划等多个子任务

    整合三层编排智能体的结果，生成完整行程计划
    """

    def __init__(self, name: str = "ItineraryPlanningAgent", model=None, skills_root=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader(skills_root)

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content={}, role="assistant")

        # 解析输入内容
        content = x.content if not isinstance(x, list) else x[-1].content

        # 初始化变量
        user_query = ""
        context_info = {}
        previous_results = []
        user_preferences = {}

        # 如果content是JSON字符串，解析它（来自OrchestrationAgent）
        if isinstance(content, str):
            try:
                data = json.loads(content)
                context_info = data.get("context", {})
                user_query = context_info.get("rewritten_query", "")
                previous_results = data.get("previous_results", [])
                user_preferences = context_info.get("user_preferences", {})
            except json.JSONDecodeError:
                user_query = content
        elif isinstance(content, dict):
            context_info = content
            user_query = content.get("rewritten_query", str(content))
            user_preferences = content.get("user_preferences", {})

        # 整合所有可用信息
        all_info = {
            "user_query": user_query,
            "context": context_info,
        }

        # 从previous_results中提取其他agent的数据
        for prev in previous_results:
            agent_name = prev.get("agent_name", "")
            result_data = prev.get("result", {}).get("data", {})
            if result_data and agent_name:
                all_info[agent_name] = result_data

        # 构建用户偏好信息
        preferences_info = ""
        if user_preferences:
            pref_parts = ["【用户偏好】（规划时优先考虑）"]
            if user_preferences.get("home_location"):
                pref_parts.append(f"• 家庭住址: {user_preferences['home_location']}")
            if user_preferences.get("hotel_brands"):
                pref_parts.append(f"• 酒店偏好: {', '.join(user_preferences['hotel_brands'])}")
            if user_preferences.get("airlines"):
                pref_parts.append(f"• 航空偏好: {', '.join(user_preferences['airlines'])}")
            if user_preferences.get("seat_preference"):
                pref_parts.append(f"• 座位偏好: {user_preferences['seat_preference']}")

            if len(pref_parts) > 1:
                preferences_info = "\n".join(pref_parts) + "\n\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        current_month = datetime.now().month
        current_season = "冬季" if current_month in [12, 1, 2] else \
                        "春季" if current_month in [3, 4, 5] else \
                        "夏季" if current_month in [6, 7, 8] else "秋季"
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 尝试从 SKILL.md 动态读取详细指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("plan-trip")
        if not skill_instruction:
            # Fallback: 如果读取失败，使用默认的简单指令
            skill_instruction = "请根据用户需求和偏好生成行程规划。"

        prompt = f"""你是一个高级行程规划专家。

【当前时间】
{current_date} {weekday}，当前季节是{current_season}

【用户需求】
{user_query}

{preferences_info}【所有收集的信息】
{json.dumps(all_info, ensure_ascii=False, indent=2)}

【任务说明与指南】
{skill_instruction}

严格只输出以下 JSON 契约，不要使用 trip、daily_schedule 等替代字段：
{{
  "itinerary": {{
    "title": "",
    "duration": "",
    "transport_recommendation": {{"preferred": "", "reason": "", "verification": ""}},
    "lodging_advice": "",
    "daily_plans": [{{"day": 1, "date": "", "activities": [{{"time": "", "activity": "", "description": "", "transport": ""}}]}}],
    "notes": [],
    "reimbursement_checklist": [],
    "missing_info": []
  }},
  "planning_complete": true
}}
"""

        try:
            # 调用模型 - 使用消息列表格式
            response = await self.model([
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本
            text = await extract_json_from_async_response(response)

            # 解析结果
            result = None
            
            # 策略1: 尝试标准解析 (依赖 robust_json_parse 的清洗能力)
            try:
                result = robust_json_parse(text, fallback=None)
            except Exception:
                # 策略2: 使用 raw_decode 解析前缀 JSON (最强力，能忽略尾随文本如 Thinking)
                try:
                    # 再次清理 Markdown (以防 extract_json_from_async_response 漏网)
                    clean_text = text
                    if "```" in clean_text:
                        import re
                        clean_text = re.sub(r'```json\s*', '', clean_text, flags=re.IGNORECASE)
                        clean_text = re.sub(r'```', '', clean_text)
                    
                    clean_text = clean_text.strip()
                    start_idx = clean_text.find('{')
                    
                    if start_idx != -1:
                        # 从第一个 { 开始尝试解析
                        clean_text = clean_text[start_idx:]
                        decoder = json.JSONDecoder()
                        obj, _ = decoder.raw_decode(clean_text)
                        result = obj
                    else:
                        raise ValueError("No JSON object start '{' found")
                except Exception as decode_err:
                    # 如果策略2也失败，抛出包含详细信息的异常
                    raise ValueError(f"All JSON parsing attempts failed. Strategy 2 error: {decode_err}")

            if result is None:
                raise ValueError("Parsed result is None")
            result = normalize_planning_result(result)

        except ExecutionLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Itinerary planning failed: {e}")
            # Ensure text is defined for logging even if extraction failed
            # 使用 locals().get 安全获取 text，防止 UnboundLocalError
            raw_text = locals().get('text', 'N/A')
            logger.error(f"Raw response text (first 500 chars): {str(raw_text)[:500]}")

            # 构建用户友好的错误消息
            error_detail = str(e)
            if "JSON" in error_detail or "parse" in error_detail.lower():
                user_message = "抱歉，模型返回的数据格式有误，无法解析行程信息。请稍后重试或简化您的需求描述。"
            else:
                user_message = f"行程规划过程中出现问题：{error_detail}"

            result = {
                "itinerary": {
                    "title": "行程规划",
                    "duration": "待完善",
                    "daily_plans": []
                },
                "planning_complete": False,
                "error": user_message,
                "technical_error": str(e)  # 保留技术细节用于调试
            }

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
