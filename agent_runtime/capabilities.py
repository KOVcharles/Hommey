"""Model-facing discovery derived from the executable tool registry, never state."""
from .control import failure
from .context_window import main_model_context
from .profiles import PROFILES
from .render import service_introduction
from .services import TOOLS, tool_schemas


DISCOVERY_ERRORS = frozenset({"UNKNOWN_TOOL", "TOOL_NOT_AVAILABLE", "SKILL_RESOURCE_NOT_FOUND"})


def tool_usage(tool):
    function = tool["function"]
    parameters = function["parameters"]
    required = set(parameters.get("required", []))
    args = []
    for name, prop in parameters.get("properties", {}).items():
        kind = "|".join(map(str, prop["enum"])) if "enum" in prop else prop.get("type", "value")
        args.append(f"{name}{'' if name in required else '?'}: {kind}")
    return f"{function['name']}({', '.join(args)}) — {function['description']}"


def capability_prompt(tools, role):
    lines = ["当前可直接调用的工具（? 表示可选参数；完整参数以原生工具 schema 为准）："]
    lines.extend(tool_usage(tool) for tool in tools)
    if role is None and any(t["function"]["name"] == "delegate" for t in tools):
        lines.append("以下是子 Agent 能力；通过 delegate(role, task, result_ids?) 委派，不能直接调用其业务工具。task 写清本次需要完成的全部事项与限制：")
        for name, profile in PROFILES.items():
            lines.append(f"{name}（{profile.title}）；Skill：{', '.join(profile.skills)}")
            lines.extend("  " + tool_usage(tool) for tool in tool_schemas(profile.tools) if tool["function"]["name"] != "read_source")
    if any(t["function"]["name"] == "read_skill" for t in tools):
        lines.append("read_skill 读取业务指南，不查询天气/车次等事实。name 是 Skill 名；resource 留空读取入口，随后只使用返回的 available_resources 路径。空列表表示没有参考文件，不要猜路径或把另一个 Skill 名填入 resource。")
    if role is None and any(t["function"]["name"] == "finish" for t in tools):
        lines.append("可直接交付的服务介绍（由运行时生成，不从历史业务报告推断）：\n" + service_introduction())
        lines.append("交付以上介绍时调用 finish(kind='help')。仅有服务介绍时 result_ids 留空；若本轮还要求业务结论，另外选择对应报告。")
    return "\n".join(lines)


def model_context(messages, tools, role, *, current_result_ids=(), request_id=None):
    # Build only for this invocation; checkpoints keep one canonical transcript.
    if role is None:
        messages = main_model_context(messages, current_result_ids, request_id)
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
