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
