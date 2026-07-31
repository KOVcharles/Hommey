#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Hommey 商旅助手 - Web 界面
基于 Gradio 的精美聊天对话框
"""
import asyncio
import json
import logging
import os
import sys
import uuid

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

import gradio as gr

# Legacy Gradio entry point. New WebUI development should target webui_new/.
from agents.intention_agent import IntentionAgent
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent
from core.intent_catalog import INTENT_DISPLAY_NAMES
from settings import LLM_CONFIG, SYSTEM_CONFIG, RESILIENCE_CONFIG
from config_agentscope import init_agentscope
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff

logger = logging.getLogger(__name__)

# ── 页面样式 ──────────────────────────────────────────────
CUSTOM_CSS = """
:root {
  --primary: #2563eb;
  --primary-light: #3b82f6;
  --bg: #f8fafc;
  --card-bg: #ffffff;
  --text: #1e293b;
  --text-dim: #64748b;
  --border: #e2e8f0;
  --success: #10b981;
  --warning: #f59e0b;
  --error: #ef4444;
}
.dark {
  --bg: #0f172a;
  --card-bg: #1e293b;
  --text: #f1f5f9;
  --text-dim: #94a3b8;
  --border: #334155;
}
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg) !important;
}
.gradio-container {
  max-width: 900px !important;
  margin: 0 auto !important;
  padding: 20px 16px !important;
  background: transparent !important;
}
#chatbot {
  min-height: 500px;
  max-height: 600px;
  border-radius: 16px !important;
  border: 1px solid var(--border) !important;
  background: var(--card-bg) !important;
  box-shadow: 0 4px 24px rgba(0,0,0,0.06) !important;
}
#chatbot .message {
  border-radius: 12px !important;
  margin: 4px 0 !important;
}
#chatbot .user {
  background: var(--primary) !important;
  color: white !important;
}
#chatbot .assistant {
  background: #f1f5f9 !important;
  color: var(--text) !important;
}
.dark #chatbot .assistant {
  background: #334155 !important;
}
#textbox {
  border-radius: 12px !important;
  border: 1px solid var(--border) !important;
  background: var(--card-bg) !important;
  color: var(--text) !important;
  font-size: 15px !important;
  padding: 12px 16px !important;
}
#textbox:focus {
  border-color: var(--primary-light) !important;
  box-shadow: 0 0 0 3px rgba(37,99,235,0.15) !important;
}
label {
  font-weight: 600 !important;
  color: var(--text) !important;
  font-size: 14px !important;
}
.btn-primary {
  border-radius: 12px !important;
  background: var(--primary) !important;
  color: white !important;
  border: none !important;
  font-weight: 600 !important;
  font-size: 15px !important;
  padding: 8px 24px !important;
  transition: all 0.2s !important;
}
.btn-primary:hover {
  background: #1d4ed8 !important;
  transform: translateY(-1px) !important;
  box-shadow: 0 4px 12px rgba(37,99,235,0.3) !important;
}
footer {
  display: none !important;
}
h1 {
  text-align: center;
  font-weight: 700 !important;
  color: var(--text) !important;
  margin-bottom: 4px !important;
}
.subtitle {
  text-align: center;
  color: var(--text-dim) !important;
  font-size: 14px;
  margin-bottom: 20px !important;
}
.agent-badge {
  display: inline-block;
  font-size: 12px;
  background: #e0e7ff;
  color: #4338ca;
  padding: 2px 10px;
  border-radius: 20px;
  margin: 2px 4px;
}
.dark .agent-badge {
  background: #1e3a5f;
  color: #93c5fd;
}
"""

# ── 智能体显示名称映射（统一来源：core.intent_catalog）────────────────────────────────────
AGENT_DISPLAY_NAMES = INTENT_DISPLAY_NAMES


class WebHommey:
    """Web 版 Hommey 商旅助手"""

    _instances: dict = {}  # 多用户实例缓存 {user_id: WebHommey}

    def __init__(self):
        self.user_id = "web_user"
        self.session_id = str(uuid.uuid4())[:8]
        self.memory_manager = None
        self.orchestrator = None
        self.intention_agent = None
        self.model = None
        self._agent_cache = {}
        self.circuit_breaker = None
        self.initialized = False

    async def initialize(self):
        """初始化系统组件（同 CLI 版，无交互式提示）"""
        init_agentscope()

        # 初始化模型
        timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
        from agentscope.model import OpenAIChatModel

        raw_model = OpenAIChatModel(
            model_name=LLM_CONFIG["model_name"],
            api_key=LLM_CONFIG["api_key"],
            client_kwargs={
                "base_url": LLM_CONFIG["base_url"],
                "timeout": float(timeout_sec),
            },
            generate_kwargs={
                "temperature": LLM_CONFIG.get("temperature", 0.7),
                "max_tokens": LLM_CONFIG.get("max_tokens", 8192),
            },
        )
        from core.execution_budget import BudgetedModel
        self.model = BudgetedModel(raw_model)

        # 记忆管理器
        self.memory_manager = MemoryManager(
            user_id=self.user_id,
            session_id=self.session_id,
            llm_model=self.model,
        )

        # 意图识别
        self.intention_agent = IntentionAgent(
            name="IntentionAgent",
            model=self.model,
        )

        # 懒加载注册器
        lazy_registry = LazyAgentRegistry(
            model=self.model,
            cache=self._agent_cache,
            memory_manager=self.memory_manager,
        )

        # 协调器
        self.orchestrator = OrchestrationAgent(
            name="OrchestrationAgent",
            agent_registry=lazy_registry,
            memory_manager=self.memory_manager,
        )

        # 熔断器
        rc = RESILIENCE_CONFIG
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=rc.get("circuit_failure_threshold", 5),
            recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
            half_open_successes=rc.get("circuit_half_open_successes", 2),
        )

        self.initialized = True

    # ── 核心处理方法 ──────────────────────────────────

    async def _get_long_term_summary(self, user_input: str = "") -> str:
        """生成长期记忆摘要（同 CLI 版）"""
        summary_parts = []

        # 偏好信息
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆）"]
            for pref_key, pref_value in prefs.items():
                if pref_value:
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        # 历史会话总结
        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        # 历史行程
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            relevant_trips = []
            other_trips = []
            for trip in all_trips:
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)
            trips_to_show = relevant_trips[:2] + other_trips[:1]
            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")
                    mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(f"{i}. {mark}{origin} → {destination} ({start_date}) - {purpose}")

        return "\n".join(summary_parts) if summary_parts else ""

    async def process(self, user_input: str) -> str:
        """处理用户输入，返回响应文本"""
        from agentscope.message import Msg

        # 长期记忆 + 短期记忆 + 意图识别
        long_term_summary = await self._get_long_term_summary(user_input)
        recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)

        context_messages = []
        if long_term_summary:
            context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
        for msg in recent_context:
            context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
        context_messages.append(Msg(name="user", content=user_input, role="user"))

        # 意图识别
        rc = RESILIENCE_CONFIG
        max_retries = rc.get("agent_max_retries", 1)

        try:
            if self.circuit_breaker:
                self.circuit_breaker.raise_if_open()

            intention_result = await retry_with_backoff(
                lambda: self.intention_agent.reply(context_messages),
                max_retries=max_retries,
                base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
            )
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
        except CircuitOpenError:
            return "⚠ 服务暂时不可用，请稍后再试。"
        except Exception as e:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
            logger.error(f"Intention agent failed: {e}")
            return f"❌ 处理请求时出错: {e}"

        try:
            intention_data = json.loads(intention_result.content)
        except json.JSONDecodeError:
            return "😅 抱歉，我没能理解您的意思，请换一种说法试试？"

        # 保存用户输入到短期记忆
        self.memory_manager.add_message("user", user_input)

        # 调度执行
        try:
            orchestration_result = await self.orchestrator.reply(intention_result)
            if self.circuit_breaker:
                self.circuit_breaker.record_success()
        except CircuitOpenError:
            return "⚠ 服务暂时不可用，请稍后再试。"
        except Exception as e:
            if self.circuit_breaker:
                self.circuit_breaker.record_failure()
            logger.error(f"Orchestration failed: {e}")
            return f"❌ 调度执行失败: {e}"

        try:
            result_data = json.loads(orchestration_result.content)
        except json.JSONDecodeError:
            result_data = {"error": "解析结果失败"}

        # 兜底：无智能体调度时走闲聊
        if result_data.get("status") == "no_agents" and not result_data.get("results"):
            if result_data.get("message"):
                response = result_data["message"]
                self.memory_manager.add_message("assistant", response)
                return response
            response = await self._handle_chitchat(user_input)
            self.memory_manager.add_message("assistant", response)
            return response

        # 生成响应文本
        response = self._format_response(result_data)
        self.memory_manager.add_message("assistant", json.dumps(result_data, ensure_ascii=False))
        return response

    async def _handle_chitchat(self, user_input: str) -> str:
        """闲聊兜底"""
        from agentscope.message import Msg

        agent = None
        try:
            agent = self.orchestrator.agent_registry["chitchat"]
        except (KeyError, Exception):
            pass

        if agent is None:
            try:
                script_path = os.path.join(
                    project_root, ".claude", "skills", "chitchat", "script", "agent.py"
                )
                import importlib.util

                spec = importlib.util.spec_from_file_location("chitchat_agent", script_path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    agent = module.ChitchatAgent(name="ChitchatAgent", model=self.model)
            except Exception as e:
                logger.warning(f"Failed to load ChitchatAgent: {e}")

        if agent is None:
            return "嗯嗯，我听着呢～有什么出行相关的问题需要帮忙吗？😊"

        try:
            input_msg = Msg(
                name="user",
                content=json.dumps({"query": user_input}, ensure_ascii=False),
                role="user",
            )
            response = await agent.reply(input_msg)
            data = json.loads(response.content) if isinstance(response.content, str) else response.content
            reply = data.get("response", "") if isinstance(data, dict) else str(data)
            return reply
        except Exception as e:
            logger.warning(f"Chitchat failed: {e}")
            return "嗯嗯，我听着呢～有什么出行相关的问题需要帮忙吗？😊"

    def _format_response(self, result_data: dict) -> str:
        """将智能体结果格式化为文本"""
        results = result_data.get("results", [])
        if not results:
            return "✓ 好的，我已收到。"

        lines = []
        # 显示调用的智能体
        agents_called = []
        for r in results:
            name = r.get("agent_name", "")
            status = r.get("status", "")
            display = AGENT_DISPLAY_NAMES.get(name, name)
            if status == "success":
                agents_called.append(f"✅ {display}")
            elif status == "error":
                agents_called.append(f"❌ {display}")
            else:
                agents_called.append(f"⚙️ {display}")

        if agents_called:
            lines.append("┃ " + "  ".join(agents_called))
            lines.append("")

        # 逐个智能体格式化结果
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})

            if status == "error":
                error_msg = data.get("error", "未知错误")
                display = AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
                lines.append(f"❌ {display} 执行失败: {error_msg}")
                continue
            if status != "success":
                continue

            text = self._format_agent_result(agent_name, data)
            if text:
                lines.append(text)

        return "\n".join(lines).strip()

    def _format_agent_result(self, agent_name: str, data: dict) -> str:
        """格式化单个智能体的输出"""
        # 行程规划
        if agent_name == "itinerary_planning":
            itinerary = data.get("itinerary")
            if not itinerary and "data" in data and isinstance(data["data"], dict):
                itinerary = data["data"].get("itinerary")
            if itinerary:
                parts = [f"✈️ **{itinerary.get('title', '行程规划')}**"]
                parts.append(f"  时长: {itinerary.get('duration', '未知')}\n")
                for day_plan in itinerary.get("daily_plans", []):
                    day_num = day_plan.get("day", 1)
                    parts.append(f"**第 {day_num} 天**")
                    activities = day_plan.get("activities") or day_plan.get("time_slots") or []
                    for slot in activities:
                        time = slot.get("time", "")
                        activity = slot.get("activity") or slot.get("location") or ""
                        description = slot.get("description", "")
                        transport = slot.get("transport", "")
                        parts.append(f"  {time} - {activity}")
                        if description:
                            parts.append(f"  _{description}_")
                        if transport:
                            parts.append(f"  🚇 {transport}")
                    meals = day_plan.get("meals", {})
                    if meals:
                        if meals.get("lunch"):
                            parts.append(f"  🍜 {meals['lunch']}")
                        if meals.get("dinner"):
                            parts.append(f"  🍽️ {meals['dinner']}")
                    parts.append("")
                notes = itinerary.get("notes", [])
                if notes:
                    parts.append("📌 **注意事项**")
                    for note in notes:
                        parts.append(f"  • {note}")
                return "\n".join(parts)

        # 偏好管理
        if agent_name == "preference":
            raw_prefs = data.get("preferences")
            if not raw_prefs and "data" in data and isinstance(data["data"], dict):
                raw_prefs = data["data"].get("preferences")
            if isinstance(raw_prefs, dict):
                prefs_list = raw_prefs.get("preferences", [])
            else:
                prefs_list = raw_prefs if isinstance(raw_prefs, list) else []
            if prefs_list:
                parts = ["✓ **已更新您的偏好设置**"]
                type_names = {
                    "home_location": "常驻地",
                    "transportation_preference": "交通偏好",
                    "hotel_brands": "酒店偏好",
                    "airlines": "航空公司偏好",
                    "seat_preference": "座位偏好",
                    "meal_preference": "餐食偏好",
                    "budget_level": "预算等级",
                }
                for pref in prefs_list:
                    pref_type = pref.get("type", "")
                    pref_value = pref.get("value", "")
                    action = pref.get("action", "replace")
                    display_type = type_names.get(pref_type, pref_type)
                    action_text = "追加" if action == "append" else "设置为"
                    parts.append(f"  • {display_type} {action_text} `{pref_value}`")
                return "\n".join(parts)

        # 事项收集
        if agent_name == "event_collection":
            origin = data.get("origin") or data.get("data", {}).get("origin")
            destination = data.get("destination") or data.get("data", {}).get("destination")
            start_date = data.get("start_date") or data.get("data", {}).get("start_date")
            end_date = data.get("end_date") or data.get("data", {}).get("end_date")
            missing_info = data.get("missing_info") or data.get("data", {}).get("missing_info") or []
            parts = []
            has_itinerary = False  # 简化处理
            if not has_itinerary and (destination or origin):
                parts.append("✓ **已收集行程信息**")
                if origin:
                    parts.append(f"  • 出发地: `{origin}`")
                if destination:
                    parts.append(f"  • 目的地: `{destination}`")
                if start_date:
                    parts.append(f"  • 出发日期: `{start_date}`")
                if end_date:
                    parts.append(f"  • 返程日期: `{end_date}`")
            if missing_info:
                parts.append(f"💡 还需要补充: {', '.join(missing_info)}")
            return "\n".join(parts) if parts else ""

        # 信息查询
        if agent_name == "information_query":
            query_results = data.get("results")
            if not query_results and "data" in data and isinstance(data["data"], dict):
                query_results = data["data"].get("results")
            if not query_results:
                query_results = data
            if not isinstance(query_results, dict):
                query_results = {}
            summary = query_results.get("summary", "")
            sources = query_results.get("sources", []) or []
            message = query_results.get("message", "")
            error = query_results.get("error", "")
            parts = []
            if summary:
                parts.append(summary)
            elif message:
                parts.append(message)
            elif error:
                parts.append(error)
            if sources:
                parts.append("\n📚 **参考来源**")
                for i, source in enumerate(sources[:3], 1):
                    url = source.get("url", "") if isinstance(source, dict) else str(source)
                    parts.append(f"  {i}. {url}")
            return "\n".join(parts) if parts else ""

        # RAG 知识库
        if agent_name == "rag_knowledge":
            answer = data.get("answer")
            if not answer and "data" in data and isinstance(data["data"], dict):
                answer = data["data"].get("answer")
            if not answer:
                answer = data.get("content") or data.get("data", {}).get("content")
            if isinstance(answer, dict):
                answer = answer.get("answer", str(answer))
            if isinstance(answer, str) and answer.strip().startswith("{") and answer.strip().endswith("}"):
                try:
                    json_obj = json.loads(answer)
                    if isinstance(json_obj, dict) and "answer" in json_obj:
                        answer = json_obj["answer"]
                except Exception:
                    pass
            if answer:
                return str(answer)

        # 记忆查询
        if agent_name == "memory_query":
            query_result = data.get("answer") or data.get("result") or data.get("content")
            if not query_result and "data" in data and isinstance(data["data"], dict):
                inner = data["data"]
                query_result = inner.get("answer") or inner.get("result") or inner.get("content")
            if query_result:
                return str(query_result)

        # 闲聊
        if agent_name == "chitchat":
            response = data.get("response") or data.get("data", {}).get("response")
            if isinstance(response, dict):
                response = response.get("response", str(response))
            if response:
                return str(response)

        # 通用兜底
        if data:
            common_keys = ["answer", "content", "result", "message", "summary", "text", "description"]
            for k in common_keys:
                if k in data and isinstance(data[k], str) and data[k].strip():
                    return data[k]
            if "data" in data and isinstance(data["data"], dict):
                for k in common_keys:
                    if k in data["data"] and isinstance(data["data"][k], str) and data["data"][k].strip():
                        return data["data"][k]

        display = AGENT_DISPLAY_NAMES.get(agent_name, agent_name)
        return f"✓ {display}已完成"

        return ""


# ── 构建 Gradio 界面 ──────────────────────────────────────
TITLE = "🌏 Hommey 商旅助手"
DESCRIPTION = "基于多智能体的智能差旅规划系统 · 让差旅更简单"


async def on_chat(message: str, history: list, uid: str):
    """处理聊天消息（流式输出）"""
    if not uid or not uid.strip():
        yield history + [["", "请输入用户ID后再开始对话。"]]
        return

    if not message or not message.strip():
        yield history + [["", "请输入您的问题。"]]
        return

    # 延迟初始化
    instance = WebHommey._instances.get(uid.strip())
    if instance is None:
        yield history + [[message, "⏳ 正在初始化系统..."]]
        instance = WebHommey()
        instance.user_id = uid.strip()
        await instance.initialize()
        WebHommey._instances[uid.strip()] = instance
        # 重新 yield 去掉初始化消息
        yield history + [[message, ""]]
    else:
        yield history + [[message, ""]]

    # 加入用户消息、开始处理
    full_history = history + [[message, ""]]

    # 显示思考中
    full_history[-1][1] = "🤔 正在分析您的问题..."
    yield full_history

    response = await instance.process(message)

    # 逐字符流式输出
    full_history[-1][1] = ""
    for char in response:
        full_history[-1][1] += char
        yield full_history
        await asyncio.sleep(0.03)


def on_start(uid: str, history: list):
    """点击「开始对话」按钮：返回欢迎消息"""
    if not uid or not uid.strip():
        return history + [["", "⚠️ 请输入有效的用户ID"]], gr.Textbox(value="")

    welcome = (
        f"👋 欢迎，**{uid.strip()}**！我是 Hommey 商旅助手。\n\n"
        "可以这样问我：\n"
        "• ✈️ 帮我规划去北京的行程\n"
        "• 🌤️ 北京的天气怎么样\n"
        "• 📋 出差住宿标准是多少\n"
        "• 👋 你好"
    )
    return history + [["", welcome]], uid.strip()


with gr.Blocks(
    title="Hommey 商旅助手",
) as demo:
    gr.HTML(f"""
    <div style="text-align:center; margin-bottom: 16px;">
        <h1 style="font-size: 28px; font-weight: 700; margin-bottom: 2px;">{TITLE}</h1>
        <p class="subtitle">{DESCRIPTION}</p>
    </div>
    """)

    # ── 用户ID 输入区 ──
    with gr.Row(equal_height=True) as setup_row:
        user_id_input = gr.Textbox(
            label="👤 用户ID",
            placeholder="请输入您的用户ID（如：zhangsan）",
            value="",
            scale=7,
            container=True,
        )
        start_btn = gr.Button(
            "🚀 开始对话",
            variant="primary",
            scale=2,
            min_width=120,
            elem_classes=["btn-primary"],
        )

    # ── 聊天区域 ──
    chatbot = gr.Chatbot(
        label="",
        height=480,
        show_label=False,
        render_markdown=True,
    )
    msg_input = gr.Textbox(
        label="",
        placeholder="输入您的需求，例如：我要从上海去北京出差",
        container=False,
        show_label=False,
    )

    # ── 快捷按钮 ──
    gr.HTML("""
    <div style="text-align:center; margin-top: 8px; font-size: 13px; color: var(--text-dim); display: flex; justify-content: center; gap: 16px; flex-wrap: wrap;">
        <span>💡 试试问：</span>
        <span class="agent-badge" style="cursor:pointer" onclick="
            const tb = document.querySelector('.gradio-container textarea, .gradio-container input');
            if(tb) { tb.value = '帮我规划去北京的行程'; tb.dispatchEvent(new Event('input')); tb.focus(); }
        ">规划行程</span>
        <span class="agent-badge" style="cursor:pointer" onclick="
            const tb = document.querySelector('.gradio-container textarea, .gradio-container input');
            if(tb) { tb.value = '北京的天气怎么样'; tb.dispatchEvent(new Event('input')); tb.focus(); }
        ">查天气</span>
        <span class="agent-badge" style="cursor:pointer" onclick="
            const tb = document.querySelector('.gradio-container textarea, .gradio-container input');
            if(tb) { tb.value = '出差住宿标准是多少'; tb.dispatchEvent(new Event('input')); tb.focus(); }
        ">差旅标准</span>
        <span class="agent-badge" style="cursor:pointer" onclick="
            const tb = document.querySelector('.gradio-container textarea, .gradio-container input');
            if(tb) { tb.value = '你好'; tb.dispatchEvent(new Event('input')); tb.focus(); }
        ">随便聊聊</span>
    </div>
    """)

    # ── 事件绑定 ──
    # 开始对话按钮 → 欢迎消息
    start_btn.click(
        fn=on_start,
        inputs=[user_id_input, chatbot],
        outputs=[chatbot, user_id_input],
    )

    # 发送消息 → 流式响应
    msg_input.submit(
        fn=on_chat,
        inputs=[msg_input, chatbot, user_id_input],
        outputs=[chatbot],
    ).then(
        fn=lambda: "",
        outputs=[msg_input],
    )


def main():
    """启动 Web 界面"""
    demo.launch(
        inbrowser=True,
        share=False,
        show_error=True,
        css=CUSTOM_CSS,
        theme=gr.themes.Soft(
            primary_hue="blue",
            neutral_hue="slate",
            font=("Inter", "sans-serif"),
        ),
    )


if __name__ == "__main__":
    main()
