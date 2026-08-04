from pathlib import Path
from typing import Dict, Optional

from settings import SKILL_CONFIG
from core.skill_definition import SkillDefinition, load_skill_definition, parse_skill_md


class SkillLoader:
    """Discover standard SKILL.md packages and merge Hommey runtime extensions."""

    def __init__(self, skills_dir: Optional[str] = None):
        project_root = Path(__file__).parent.parent.resolve()
        configured_dir = skills_dir or SKILL_CONFIG.get("root", ".agents/skills")
        self.skills_dir = Path(configured_dir)
        if not self.skills_dir.is_absolute():
            self.skills_dir = project_root / self.skills_dir
        self.skills_dir = self.skills_dir.resolve()
        self.skills: Dict[str, Dict] = {}
        self.definitions: Dict[str, SkillDefinition] = {}

    def load_skills(self) -> Dict[str, Dict]:
        """Discover skills from standard ``SKILL.md`` frontmatter."""
        if not self.skills_dir.exists():
            print(f"Warning: Skills directory {self.skills_dir} not found.")
            return {}

        definitions = self.load_definitions()

        self.skills = {
            definition.name: {
                "name": definition.name,
                "description": definition.description,
                "directory": definition.name,
            }
            for definition in definitions.values()
        }

        return self.skills

    def load_definitions(self, strict: bool = True) -> Dict[str, SkillDefinition]:
        """Load every standard Skill and its optional ``hommey.yaml`` extension."""
        if not self.skills_dir.exists():
            return {}

        definitions: Dict[str, SkillDefinition] = {}
        errors = []
        for skill_path in sorted(self.skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue
            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                errors.append(f"Missing SKILL.md: {skill_path.name}")
                continue
            try:
                definition = load_skill_definition(skill_path)
                definitions[definition.name] = definition
            except Exception as exc:
                errors.append(f"{skill_path.name}: {exc}")

        if errors and strict:
            raise ValueError("Invalid skills:\n- " + "\n- ".join(errors))
        self.definitions = definitions
        return definitions

    def get_definition(self, skill_name: str) -> Optional[SkillDefinition]:
        if not self.definitions:
            self.load_definitions()
        return self.definitions.get(skill_name)

    def load_manifests(self, strict: bool = True) -> Dict[str, SkillDefinition]:
        """Compatibility alias; use :meth:`load_definitions` in new code."""
        return self.load_definitions(strict=strict)

    def get_manifest(self, skill_name: str) -> Optional[SkillDefinition]:
        """Compatibility alias; use :meth:`get_definition` in new code."""
        return self.get_definition(skill_name)

    def get_skill_prompt(self, skill_mapping: Optional[Dict[str, str]] = None) -> str:
        if not self.skills:
            self.load_skills()

        prompt_lines = []
        for index, (name, info) in enumerate(sorted(self.skills.items()), start=1):
            display_name = skill_mapping.get(name, name) if skill_mapping else name
            desc = info.get("description", "").replace("\n", " ")
            prompt_lines.append(f"{index}. {display_name} - {desc}")

        return "\n\n".join(prompt_lines)

    def get_skill_content(self, skill_name: str) -> Optional[str]:
        if not self.skills:
            self.load_skills()

        definition = self.get_definition(skill_name)
        if not definition:
            return None
        target_path = self.skills_dir / definition.name / "SKILL.md"

        try:
            _, body = parse_skill_md(target_path)
            return body
        except Exception as exc:
            print(f"Error reading skill content {target_path}: {exc}")
            return None

    def get_skill_resource(self, skill_name: str, relative_path: str) -> Optional[str]:
        """Read a UTF-8 resource without allowing paths outside the Skill package."""
        definition = self.get_definition(skill_name)
        if not definition:
            return None
        skill_root = (self.skills_dir / definition.name).resolve()
        target_path = (skill_root / relative_path).resolve()
        try:
            target_path.relative_to(skill_root)
        except ValueError:
            raise ValueError(f"Skill resource escapes package root: {relative_path}")
        if not target_path.is_file():
            return None
        return target_path.read_text(encoding="utf-8").strip()
