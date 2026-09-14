"""Read-only catalog of guidance used by the supervisor and specialists."""
from agent_runtime.profiles import PROFILES
from utils.skill_loader import SkillLoader


class SkillPlatformService:
    def __init__(self, loader=None):
        self.loader = loader or SkillLoader()

    def list_skills(self):
        definitions = self.loader.load_definitions()
        return [{**item.model_dump(), "roles": [role for role, profile in PROFILES.items() if item.name in profile.skills]}
                for item in sorted(definitions.values(), key=lambda item: (item.catalog_order, item.name))]

    def get_skill(self, skill_name):
        skill = next((item for item in self.list_skills() if item["name"] == skill_name), None)
        if skill is not None:
            skill["instructions"] = self.loader.get_skill_content(skill_name) or ""
        return skill
