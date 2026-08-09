"""Standard Agent Skill metadata plus Hommey runtime extensions."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkillFrontmatter(BaseModel):
    """Metadata read from the standard ``SKILL.md`` YAML frontmatter."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    description: str = Field(min_length=1, max_length=1024)
    license: Optional[str] = None
    compatibility: Optional[str] = Field(default=None, min_length=1, max_length=500)
    metadata: Dict[str, str] = Field(default_factory=dict)
    allowed_tools: Optional[str] = Field(default=None, alias="allowed-tools")


class SkillDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    required: bool = True
    purpose: str = ""


class SkillExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    agent_name: str
    priority: int = Field(ge=1)
    reason: str = ""
    expected_output: str = ""
    on_failure: Literal["abort", "continue"] = "abort"
    max_retries: int = Field(default=0, ge=0, le=2)
    # 步骤级 scoped query 模板；占位符 {origin}/{destination}/{start_date}/{duration}/{purpose}
    # 由 TaskGraphBuilder 用 key_entities + 上游证据渲染，保证每步 query 不被其他意图污染。
    query: Optional[str] = None
    # 结果判定规则，例如 {"error_when_field": "query_success", "error_code": "..."}。
    # 取代 executor 中按 intent 硬编码的特殊状态分支。
    result_rules: Optional[Dict[str, Any]] = None


class SkillScope(BaseModel):
    """Validator 词域检查：query 不得引入超出原始请求的事实。"""

    model_config = ConfigDict(extra="forbid")

    forbidden_terms: List[str] = Field(default_factory=list)
    expansion_terms: List[str] = Field(default_factory=list)
    # Values already recognized from the request that must survive LLM task
    # decomposition for this Skill. The validator restores them without
    # inventing any new facts or intent scope.
    query_anchor_fields: List[Literal[
        "origin", "destination", "start_date", "duration", "purpose",
    ]] = Field(default_factory=list)


class AnswerSpec(BaseModel):
    """Composer 契约：意图 → AnswerSection 的映射与展示规则。"""

    model_config = ConfigDict(extra="forbid")

    section_kind: Optional[Literal[
        "policy", "weather", "memory", "preference", "trip", "notice", "general"
    ]] = None
    require_section: bool = True
    # 主 agent 成功后隐藏这些工作流中间 agent 的结果（例如 plan-trip 完成后
    # 不单独展示 event_collection / rag_knowledge / information_query）。
    suppress_agents: List[str] = Field(default_factory=list)
    primary_agent: Optional[str] = None


class PauseSpec(BaseModel):
    """节点级等待用户输入声明；它不拥有全局运行状态。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    pause_agent: Optional[str] = None
    pause_field: str = "planning_ready"


class MemoryHook(BaseModel):
    """声明式记忆回写：哪个 agent 的结果触发哪种副作用。"""

    model_config = ConfigDict(extra="forbid")

    agent: str
    effect: Literal["update_active_trip", "save_preference", "complete_trip"]
    require_field: Optional[str] = None


class HommeySkillConfig(BaseModel):
    """Optional ``hommey.yaml`` fields used only by the Hommey runtime."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    display_name: Optional[str] = None
    category: Literal["business", "workflow", "capability", "interaction"] = "capability"
    domain: str = "business-travel"
    intent: Optional[str] = None
    agent_name: Optional[str] = None
    entrypoint: str = "script/agent.py"
    user_facing: bool = True
    enabled_by_default: bool = True
    risk_level: Literal["low", "medium", "high"] = "low"
    catalog_order: int = 100
    tools: List[Literal[
        "active_trip_context", "rag_retrieval", "travel_information",
        "weather", "web_search", "memory", "mcp",
    ]] = Field(default_factory=list)
    requires: List[SkillDependency] = Field(default_factory=list)
    execution: List[SkillExecutionStep] = Field(default_factory=list)
    input_schema: Optional[str] = None
    output_schema: Optional[str] = None
    # 声明式编排元数据（全部可选，向后兼容；新增 skill 只改 hommey.yaml）
    confidence_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    scope: Optional[SkillScope] = None
    side_effect_allowed: bool = False
    answer: Optional[AnswerSpec] = None
    pause: Optional[PauseSpec] = None
    memory_hooks: List[MemoryHook] = Field(default_factory=list)
    progress_key: Optional[str] = None
    updates_preferences: bool = False

    @model_validator(mode="after")
    def validate_runtime_contract(self):
        if self.intent and not self.agent_name:
            raise ValueError("intent-backed skills require agent_name")
        if self.intent and not self.execution:
            raise ValueError("intent-backed skills require an execution plan")
        return self


class SkillDefinition(HommeySkillConfig):
    """Merged standard metadata and optional Hommey runtime configuration."""

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    description: str = Field(min_length=1, max_length=1024)
    license: Optional[str] = None
    compatibility: Optional[str] = Field(default=None, min_length=1, max_length=500)
    metadata: Dict[str, str] = Field(default_factory=dict)
    allowed_tools: Optional[str] = None
    display_name: str
    hommey_configured: bool = Field(default=False, exclude=True)

    def validate_resources(self, skill_dir: Path) -> None:
        entrypoint = skill_dir / self.entrypoint
        if self.agent_name and not entrypoint.exists():
            raise ValueError(f"Missing skill entrypoint: {entrypoint}")
        for relative in (self.input_schema, self.output_schema):
            if relative and not (skill_dir / relative).exists():
                raise ValueError(f"Missing skill schema: {skill_dir / relative}")


def parse_skill_md(path: Path) -> Tuple[SkillFrontmatter, str]:
    """Parse standard YAML frontmatter and return metadata plus Markdown body."""
    content = path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"SKILL.md must start with YAML frontmatter: {path}")

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise ValueError(f"SKILL.md frontmatter is not closed: {path}")

    raw_metadata = yaml.safe_load("\n".join(lines[1:closing_index]))
    if not isinstance(raw_metadata, dict):
        raise ValueError(f"SKILL.md frontmatter must be a mapping: {path}")
    metadata = SkillFrontmatter.model_validate(raw_metadata)
    if metadata.name != path.parent.name:
        raise ValueError(
            f"Skill name '{metadata.name}' must match directory '{path.parent.name}'"
        )

    body = "\n".join(lines[closing_index + 1:]).strip()
    if not body:
        raise ValueError(f"SKILL.md must contain instructions after frontmatter: {path}")
    return metadata, body


def load_skill_definition(skill_dir: Path) -> SkillDefinition:
    """Load a standard Skill and merge its optional Hommey runtime extension."""
    metadata, _ = parse_skill_md(skill_dir / "SKILL.md")
    config_path = skill_dir / "hommey.yaml"
    raw_config: Dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Hommey skill config must be a mapping: {config_path}")
        raw_config = loaded

    config = HommeySkillConfig.model_validate(raw_config)
    definition = SkillDefinition.model_validate({
        **config.model_dump(),
        **metadata.model_dump(),
        "display_name": config.display_name or metadata.name,
        "hommey_configured": config_path.exists(),
    })
    definition.validate_resources(skill_dir)
    return definition
