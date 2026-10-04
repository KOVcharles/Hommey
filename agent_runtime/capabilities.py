"""Model-facing discovery derived from the executable tool registry, never state."""
from .control import failure
from .context_window import main_model_context, model_messages
from .profiles import PROFILES
from .services import TOOLS


DISCOVERY_ERRORS = frozenset({"UNKNOWN_TOOL", "TOOL_NOT_AVAILABLE", "SKILL_RESOURCE_NOT_FOUND"})


def capability_prompt(tools, role):
    lines = []
    if role is None and any(t["function"]["name"] == "delegate" for t in tools):
        lines.append("可委派角色（通过 delegate 分配任务，保留用户限制与必要依赖）：")
        for name, profile in PROFILES.items():
            lines.append(f"{name}（{profile.title}）；Skill：{', '.join(profile.skills)}")
    return "\n".join(lines)


def model_context(messages, tools, role, *, current_result_ids=(), rules=None, snapshot=None, feedback=None):
    if rules is not None:
        catalog = capability_prompt(tools, role)
        return model_messages(rules + ("\n\n" + catalog if catalog else ""), messages,
                              snapshot=snapshot, feedback=feedback)
    # Build only for this invocation; checkpoints keep one canonical transcript.
    if role is None:
        messages = main_model_context(messages, current_result_ids)
    return [{**messages[0], "content": messages[0]["content"] + "\n\n" + capability_prompt(tools, role)}, *messages[1:]]


def unavailable_tool(name, available, known, role):
    exists = name in known or name in TOOLS or name == "report"
    message = f"工具 {name} 当前不可直接调用。" if exists else f"工具 {name} 不存在。"
    message += "请从 available_tools 选择正确工具并修正调用；这是工具选择错误，不是缺少用户信息。"
    roles = [key for key, profile in PROFILES.items() if name in profile.tools]
    if role is None and roles and "delegate" in available:
        message += "该能力通过 delegate 委派给 " + "、".join(roles) + "。"
    return {**failure("TOOL_NOT_AVAILABLE" if exists else "UNKNOWN_TOOL", message),
            "available_tools": sorted(available)}
