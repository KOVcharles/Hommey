"""意图 ↔ Skill 单一目录（single source of truth）。

每个 skill-backed 意图 1:1 对应一个 skill 目录；非 skill 意图（unclear / unsupported）
不调用任何 skill，由主流程直接处理。

消费方：
- agents/intention_agent.py —— 用 build_intent_prompt_section() 渲染 prompt 的意图列表，
  用 intent_to_skill() 替代本地 skill_mapping。
- core/intent_guard.py / core/intent_router.py —— 用 CHITCHAT_EXACT / CHITCHAT_KEYWORDS
  识别寒暄，并统一路由到 chitchat skill。
- webui_new/manager.py —— 用 INTENT_DISPLAY_NAMES 统一中文标签。

一致性由 tests/test_intent_catalog.py 保证：意图目录必须覆盖所有声明了
Hommey intent 的 Skill；只有标准 `SKILL.md`、没有意图扩展的包不参与该约束。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Tuple

from utils.skill_loader import SkillLoader

# skill-backed 意图：intent_name -> 元信息
# 顺序即 prompt 中展示的顺序；description 为面向意图分类的简洁中文说明。
def _load_skill_intents() -> Dict[str, Dict[str, str]]:
    definitions = SkillLoader().load_definitions()
    ordered = sorted(definitions.values(), key=lambda item: (item.catalog_order, item.name))
    return {
        definition.intent: {
            "skill": definition.name,
            "description": definition.description,
            "display": definition.display_name,
        }
        for definition in ordered
        if definition.intent
    }


SKILL_INTENTS: Dict[str, Dict[str, str]] = _load_skill_intents()

# 非 skill 意图：不调用任何 skill，由主流程直接处理
NON_SKILL_INTENTS: Dict[str, Dict[str, str]] = {
    "unclear": {
        "description": "输入太短、模糊、半句话或无法确定意图，不调用 skill",
        "display": "需澄清",
    },
    "unsupported": {
        "description": "与公司差旅无关或系统不执行的请求，不调用 skill",
        "display": "不支持",
    },
}


def all_intents() -> Dict[str, Dict[str, str]]:
    """全部意图（skill-backed + 非 skill）。"""
    return {**SKILL_INTENTS, **NON_SKILL_INTENTS}


# 反查：skill 目录名 -> intent_name
_SKILL_TO_INTENT: Dict[str, str] = {
    info["skill"]: intent for intent, info in SKILL_INTENTS.items()
}


def intent_to_skill(intent: str) -> Optional[str]:
    """意图名 -> skill 目录名；非 skill 意图或未知意图返回 None。"""
    info = SKILL_INTENTS.get(intent)
    return info["skill"] if info else None


def skill_to_intent(skill: str) -> Optional[str]:
    """skill 目录名 -> 意图名；未知 skill 返回 None。"""
    return _SKILL_TO_INTENT.get(skill)


def is_skill_intent(intent: str) -> bool:
    return intent in SKILL_INTENTS


# 统一中文显示名（含非 skill 意图）
INTENT_DISPLAY_NAMES: Dict[str, str] = {
    intent: info["display"] for intent, info in all_intents().items()
}


def display_name(intent: str) -> str:
    """意图 -> 中文显示名；未知意图原样返回。"""
    return INTENT_DISPLAY_NAMES.get(intent, intent)


# 寒暄关键词（guard / router 共用，消除原先分散在多个模块的重叠关键词集）
# 短问候的精确匹配集 —— 必须在 guard 的 length<=2 判断之前命中，
# 否则"你好/在吗/嗨"等 1~2 字输入会被误判为 unclear。
CHITCHAT_EXACT: FrozenSet[str] = frozenset({
    "你好", "您好", "嗨", "哈喽",
    "hi", "hello", "hey",
    "在吗", "在不在", "有人吗",
    "谢谢", "感谢", "多谢",
    "再见", "拜拜", "bye",
    "ok", "okay", "好的",
    "哈哈", "呵呵", "没事", "没什么", "算了",
    "回头见", "明天见", "下次见",
    "thanks", "thank",
})

# 寒暄的子串关键词（较长输入的包含匹配）
CHITCHAT_KEYWORDS: Tuple[str, ...] = (
    "你好", "您好", "嗨", "哈喽", "在吗",
    "你是什么", "你是谁", "你叫什么", "你能做什么", "你会什么", "介绍一下",
    "谢谢", "感谢", "再见",
)


def build_intent_prompt_section() -> str:
    """渲染 prompt 中的【意图类型】列表（skill-backed + 非 skill）。"""
    lines = []
    for intent, info in SKILL_INTENTS.items():
        lines.append(f"- {intent}: {info['description']}")
    for intent, info in NON_SKILL_INTENTS.items():
        lines.append(f"- {intent}: {info['description']}（should_call_skill 必须为 false）")
    return "\n".join(lines)


# ---- 声明式派生注册表 -------------------------------------------------
# 从 skill 定义读取编排元数据，单一数据源（.agents/skills/*/hommey.yaml）。
# 新增 skill 后这些 helper 自动生效，无需改任何 Python。

_DEFINITIONS: Dict[str, object] = {
    definition.name: definition
    for definition in SkillLoader().load_definitions().values()
}


def _definition_for_intent(intent: str) -> Optional[object]:
    skill = intent_to_skill(intent)
    return _DEFINITIONS.get(skill) if skill else None


def agent_for_intent(intent: str) -> Optional[str]:
    """意图名 -> agent_name（该 intent 的 skill 声明的入口 agent）。"""
    definition = _definition_for_intent(intent)
    return getattr(definition, "agent_name", None)


def definition_for_agent(agent_name: str) -> Optional[object]:
    """agent_name -> SkillDefinition；用于 memory hook / preference 回写等反查。"""
    for definition in _DEFINITIONS.values():
        if getattr(definition, "agent_name", None) == agent_name:
            return definition
    return None


def section_kind_for_intent(intent: str) -> Optional[str]:
    """意图 -> AnswerSection.kind；未声明返回 None（composer 自行决定）。"""
    definition = _definition_for_intent(intent)
    answer = getattr(definition, "answer", None)
    return answer.section_kind if answer else None


def require_section_for_intent(intent: str) -> bool:
    """该意图是否必须在 AnswerDocument 中出现对应 section（validator 契约）。"""
    definition = _definition_for_intent(intent)
    answer = getattr(definition, "answer", None)
    return answer.require_section if answer else True


def suppress_agents_for_intent(intent: str) -> List[str]:
    """主 agent 成功后隐藏的工作流中间 agent（plan-trip 完成后折叠）。"""
    definition = _definition_for_intent(intent)
    answer = getattr(definition, "answer", None)
    return list(answer.suppress_agents) if answer else []


def primary_agent_for_intent(intent: str) -> Optional[str]:
    definition = _definition_for_intent(intent)
    answer = getattr(definition, "answer", None)
    return answer.primary_agent if answer else None


def side_effect_allowed(intent: str) -> bool:
    """该意图允许携带副作用（记忆回写等），validator 白名单依据。"""
    definition = _definition_for_intent(intent)
    return bool(getattr(definition, "side_effect_allowed", False))


def progress_key_for_intent(intent: str) -> str:
    """前端进度文案 key；未声明回退 'task_running'。"""
    definition = _definition_for_intent(intent)
    return getattr(definition, "progress_key", None) or "task_running"


def confidence_threshold_for_intent(intent: str) -> Optional[float]:
    """意图级置信度门槛；None 表示使用默认阈值。"""
    definition = _definition_for_intent(intent)
    return getattr(definition, "confidence_threshold", None)


def catalog_rank(intent: str) -> int:
    """意图展示/主意图优先级排序键（catalog_order，小的优先）。"""
    definition = _definition_for_intent(intent)
    return getattr(definition, "catalog_order", 100)


def updates_preferences_for_agent(agent_name: str) -> bool:
    """该 agent 成功后是否应把 preference_updated 置 True（前端标记）。"""
    definition = definition_for_agent(agent_name)
    return bool(definition and getattr(definition, "updates_preferences", False))


def pause_spec_for_intent(intent: str) -> Optional[object]:
    """节点级等待用户输入声明；未声明返回 None。"""
    definition = _definition_for_intent(intent)
    return getattr(definition, "pause", None)


def memory_hooks_for_intent(intent: str) -> List[object]:
    """意图声明的记忆回写 hook（MemoryHook 列表）。"""
    definition = _definition_for_intent(intent)
    return list(getattr(definition, "memory_hooks", []) or [])


def execution_steps_for_intent(intent: str) -> List[object]:
    """意图的 skill execution 模板步骤（SkillExecutionStep 列表）。"""
    definition = _definition_for_intent(intent)
    return list(getattr(definition, "execution", []) or [])


def result_rules_for_intent(intent: str) -> Dict[str, object]:
    """意图首个步骤的 result_rules（executor 状态判定用）。"""
    steps = execution_steps_for_intent(intent)
    if steps:
        rules = getattr(steps[0], "result_rules", None)
        return dict(rules) if rules else {}
    return {}


def intent_api_payload() -> Dict[str, Dict[str, str]]:
    """``GET /api/intents`` 的声明式载荷：前端进度标签无需硬编码意图名。"""
    return {
        intent: {
            "display": info["display"],
            "progress_key": progress_key_for_intent(intent),
            "description": info["description"],
            "skill": info["skill"],
        }
        for intent, info in SKILL_INTENTS.items()
    }
