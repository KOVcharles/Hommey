"""
闲聊智能体 ChitchatAgent
职责：处理简短问候、感谢、告别和能力介绍，并引导回公司差旅任务

核心功能：
1. 规则模板匹配 - 覆盖 80% 常见闲聊，快速响应、离线可用
2. LLM 兜底生成 - 规则未命中时简短引导回公司差旅能力
3. 离线终级兜底 - LLM 不可用时仍可输出友善文案

设计原则：
- 规则优先：高频场景秒回，不受 LLM 配额影响
- 渐进降级：规则 → LLM → 终级兜底，三层保护
- 风格统一：轻松友好，带适当 emoji
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from core.execution_budget import ExecutionLimitExceeded
from typing import Optional, Union, List, Dict, Any
import json
import logging
import re
import random

logger = logging.getLogger(__name__)


# ============================================================
# 闲聊规则模板库
# ============================================================

def _match_any(patterns: List[str], text: str) -> bool:
    """检查文本是否匹配任一模式"""
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return True
    return False


CHITCHAT_RULES = [
    # ---- 问候类 ----
    {
        "patterns": [r"^(你好|hi|hello|嗨|哈喽|halo|hey)[\s!！。.,，]*$",
                     r"^(早上好|早啊|晚上好|下午好|中午好|早安|晚安)[\s!！。.,，]*$",
                     r"^(在吗|在不在|有人在吗|在么)[\s!！。.,，?？]*$"],
        "category": "greeting",
        "responses": [
            "你好呀！😊 我是 Hommey 商旅助手，有什么可以帮你的吗？",
            "嗨～我在呢！需要我帮你查询差旅政策、规划出差路线，还是准备报销材料？",
            "你好！有公司出差任务的话，可以把目的地、日期和工作安排告诉我。",
        ]
    },
    # ---- 状态询问 ----
    {
        "patterns": [r"你在干嘛", r"你在做什么", r"你在干什么",
                     r"你能做什么", r"你有什么功能", r"你会什么",
                     r"你叫什么", r"你是谁", r"介绍一下你自己"],
        "category": "status_inquiry",
        "responses": [
            "我在随时待命呢！💪 可以帮你：\n📋 规划出差行程\n🔍 搜索目的地信息\n🌤️ 查询天气\n📝 记录偏好习惯\n❓ 回答差旅相关问题\n\n需要我做点什么呢？",
            "我是 Hommey 企业差旅助手，可以查询公司差旅制度、规划出差路线和住宿区域，并整理报销准备事项。",
            "我可以围绕公司出差提供路线、交通、住宿、政策和报销建议，但不会执行预订、付款或审批。",
        ]
    },
    # ---- 感谢类 ----
    {
        "patterns": [r"(谢谢|多谢|感谢|3Q|thanks|thank\s*you|thx)[\s!！。.,，!]*$",
                     r"(太棒了|太好了|真棒|厉害|牛|给力).*"],
        "category": "thanks",
        "responses": [
            "不客气！能帮到你我也很开心～😊 还有什么需要吗？",
            "嘿嘿，举手之劳啦！有问题随时找我哦～✨",
            "不用谢！这就是我的工作嘛～有需要再说！💪",
        ]
    },
    # ---- 告别类 ----
    {
        "patterns": [r"(再见|拜拜|bye|see\s*you|回头见|下次见|88)[\s!！。.,，!]*$",
                     r"(我先走了|我走了|下了|先下了|撤了)"],
        "category": "goodbye",
        "responses": [
            "再见！👋 祝你一路顺风，有需要随时找我～",
            "拜拜～期待下次见面！旅途愉快哦 ✈️😊",
            "好的，先忙吧！我随时在这儿等着你，再见啦～👋",
        ]
    },
    # ---- 情绪类 ----
    {
        "patterns": [r"我好累", r"累死了", r"好疲惫", r"身心俱疲", r"好困",
                     r"好无聊", r"无聊死了", r"没事干",
                     r"好烦", r"烦死了", r"真倒霉", r"运气.*差",
                     r"好开心", r"真高兴", r"太高兴了", r"好兴奋"],
        "category": "emotion",
        "responses": {
            "tired": [
                "辛苦了辛苦了！😮‍💨 出差确实累人，要不要帮你查查目的地有没有什么放松的好去处？",
                "累的话就歇歇吧～身体最重要！需要我帮你简化一下行程安排吗？😊",
            ],
            "bored": [
                "哈哈，无聊的时候最适合计划下一次旅行了！要不要聊聊想去哪里？✈️",
                "那我给你推荐几个好玩的地方？或者讲讲你之前的旅行故事？😄",
            ],
            "upset": [
                "别烦别烦，一切都会好起来的！🍀 要不要聊聊，或者我帮你规划一段放松的旅行？",
                "生活总有不如意，但旅行总能治愈人心～想不想看看哪里有好风景？🌈",
            ],
            "happy": [
                "哇，看来今天心情不错呀！😆 有什么好事发生吗？分享分享～",
                "开心就好！好心情配上好旅程，完美～要不要趁机规划一下？✨",
            ]
        }
    },
    # ---- 肯定/否定 ----
    {
        "patterns": [r"^(好的|ok|好|可以|行|嗯|对|是的|没错|对的)[\s!！。.,，]*$",
                     r"^(不行|不要|算了|不用了|不需要|别)[\s!！。.,，]*$"],
        "category": "acknowledgment",
        "responses": [
            "好的，收到！😊",
            "嗯嗯，有什么想法随时说～",
        ]
    },
]


def _classify_emotion(text: str) -> str:
    """细分情绪类型"""
    if _match_any([r"累|疲惫|困"], text):
        return "tired"
    if _match_any([r"无聊|没事干"], text):
        return "bored"
    if _match_any([r"烦|倒霉|运气.*差"], text):
        return "upset"
    if _match_any([r"开心|高兴|兴奋|真棒"], text):
        return "happy"
    return "general"


def _apply_rules(user_query: str) -> Optional[str]:
    """应用规则模板，返回匹配的回复（未命中则返回 None）"""
    text = user_query.strip()
    if not text:
        return None

    if any(keyword in text for keyword in ("你是什么", "你是谁", "你叫什么", "你能做什么", "你会什么", "介绍一下")):
        return (
            "我是 Hommey 商旅助手，可以帮你规划差旅行程、查询天气和目的地信息、"
            "记录出行偏好，也能回答差旅标准和报销相关问题。你可以直接告诉我想去哪儿，"
            "或者问我某个城市/政策/行程安排。"
        )

    if any(keyword in text for keyword in ("你好", "您好", "嗨", "哈喽", "在吗")):
        return "你好呀！我是 Hommey 商旅助手，有出行规划、差旅政策或目的地信息都可以直接问我。"

    for rule in CHITCHAT_RULES:
        if _match_any(rule["patterns"], text):
            if rule["category"] == "emotion":
                sub = _classify_emotion(text)
                pool = rule["responses"].get(sub, rule["responses"].get("general", []))
            else:
                pool = rule["responses"]
            if pool:
                return random.choice(pool)
    return None


# ============================================================
# ChitchatAgent
# ============================================================

class ChitchatAgent(AgentBase):
    """
    闲聊智能体

    输入格式（兼容两种）：
    1. 来自 Orchestrator：{"context": {...}, "reason": "...", ...}
       → 从 context.rewritten_query 提取查询文本
    2. 来自 CLI 直接调用：{"query": "用户在干嘛"} 或纯文本
       → 直接使用

    输出格式：
    {"response": "友善的回复文本", "source": "rule"|"llm"|"fallback"}
    """

    def __init__(self, name: str = "ChitchatAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """处理闲聊请求，返回友善回复"""
        # --- 1. 提取用户查询文本 ---
        user_query = self._extract_query(x)

        if not user_query:
            return Msg(
                name=self.name,
                content=json.dumps({
                    "response": "嗯？你想说什么呀～我没太听清 😊",
                    "source": "fallback"
                }, ensure_ascii=False),
                role="assistant"
            )

        # --- 2. 规则模板匹配（最快路径，离线可用）---
        rule_response = _apply_rules(user_query)
        if rule_response:
            logger.info(f"Chitchat rule matched for: '{user_query[:50]}'")
            return Msg(
                name=self.name,
                content=json.dumps({
                    "response": rule_response,
                    "source": "rule"
                }, ensure_ascii=False),
                role="assistant"
            )

        # --- 3. LLM 兜底（需要模型可用）---
        if self.model:
            try:
                llm_response = await self._generate_llm_response(user_query)
                if llm_response:
                    return Msg(
                        name=self.name,
                        content=json.dumps({
                            "response": llm_response,
                            "source": "llm"
                        }, ensure_ascii=False),
                        role="assistant"
                    )
            except ExecutionLimitExceeded:
                raise
            except Exception as e:
                logger.warning(f"Chitchat LLM fallback failed: {e}")

        # --- 4. 终级兜底（完全离线）---
        fallback = self._get_fallback_response(user_query)
        return Msg(
            name=self.name,
            content=json.dumps({
                "response": fallback,
                "source": "fallback"
            }, ensure_ascii=False),
            role="assistant"
        )

    def _extract_query(self, x) -> str:
        """从不同输入格式中提取用户查询文本"""
        if x is None:
            return ""

        # 提取 Msg 内容
        raw = ""
        if isinstance(x, list):
            raw = x[-1].content if x else ""
        elif hasattr(x, 'content'):
            raw = x.content
        else:
            raw = str(x)

        # 尝试解析 JSON
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return raw.strip()

        if not isinstance(data, dict):
            return raw.strip()

        # 从 orchestrator 格式提取
        ctx = data.get("context", {})
        rewritten = ctx.get("rewritten_query", "") if isinstance(ctx, dict) else ""
        if rewritten and rewritten.strip():
            return rewritten.strip()

        # 从直接调用格式提取
        if "query" in data and data["query"]:
            return str(data["query"]).strip()

        # 从 reason 字段提取（最后手段）
        reason = data.get("reason", "")
        if reason:
            return str(reason).strip()

        return raw.strip()

    async def _generate_llm_response(self, user_query: str) -> Optional[str]:
        """调用 LLM 生成自然闲聊回复"""
        prompt = f"""你是一个友好的企业差旅助手，名叫 Hommey。用户正在进行简短礼貌对话。

用户说：「{user_query}」

请用轻松友好的语气回复，1-3 句话即可。风格要求：
- 回复简短自然
- 自然引导到公司差旅政策、出差路线或报销准备
- 不扩展为开放式闲聊或情绪陪伴
- 不要输出 JSON，直接输出对话文本

你的回复："""

        messages = [
            {"role": "system", "content": "你是企业差旅助手，只做简短礼貌回复并引导回公司差旅能力。"},
            {"role": "user", "content": prompt}
        ]

        try:
            response = await self.model(messages)

            text = ""
            if hasattr(response, '__aiter__'):
                async for chunk in response:
                    if isinstance(chunk, str):
                        text = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            text = chunk.content
                        elif isinstance(chunk.content, list):
                            # DeepSeek 返回 [{'type':'thinking',...}, {'type':'text',...}]
                            # 只提取 text 类型，忽略 thinking 推理过程
                            # 注意：流式每个 chunk 包含完整累积文本，用 = 覆盖而非 += 拼接
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
            elif hasattr(response, 'text'):
                text = response.text
            elif hasattr(response, 'content'):
                if isinstance(response.content, str):
                    text = response.content
                elif isinstance(response.content, list):
                    for item in response.content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                else:
                    text = str(response.content)
            elif isinstance(response, dict):
                text = response.get('content', '')
            else:
                text = str(response) if response else ""

            text = text.strip()
            # 清理可能的 JSON 包装
            if text.startswith('```'):
                lines = text.split('\n')
                text = '\n'.join(lines[1:-1]) if len(lines) > 2 else text
            return text if text else None

        except ExecutionLimitExceeded:
            raise
        except Exception as e:
            logger.error(f"Chitchat LLM generation failed: {e}")
            return None

    def _get_fallback_response(self, user_query: str) -> str:
        """终极离线兜底回复"""
        fallbacks = [
            "嗯嗯，我听着呢～😊 有什么出行相关的问题需要帮忙吗？",
            "哈哈，虽然我不太懂这个，但我很擅长规划旅行哦！要不要试试？✈️",
            "有意思～不过我平时都在想怎么帮人出差的事儿。需要我帮你规划点什么吗？",
            "😄 这个话题我不太擅长，但如果说到出行规划，我可是专业的！需要帮忙吗？",
        ]
        return random.choice(fallbacks)
