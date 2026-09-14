"""Skill documents and display metadata. Skills never contain executable workflows."""
from __future__ import annotations
from pathlib import Path
from typing import Dict, Optional, Tuple
import yaml
from pydantic import BaseModel, ConfigDict, Field

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

class SkillDefinition(SkillFrontmatter):
    version: str = "1.0.0"
    display_name: str
    category: str = "business"
    domain: str = "business-travel"
    risk_level: str = "low"
    catalog_order: int = 100


def load_skill_definition(skill_dir: Path) -> SkillDefinition:
    metadata, _ = parse_skill_md(skill_dir / "SKILL.md")
    config = skill_dir / "hommey.yaml"
    values = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    if not isinstance(values, dict):
        raise ValueError("Skill display metadata must be a mapping")
    return SkillDefinition.model_validate({"display_name": metadata.name, **values, **metadata.model_dump()})
