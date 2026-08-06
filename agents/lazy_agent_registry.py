#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Lazy plugin registry for skill-backed agents.

The runtime skill root is controlled by settings.SKILL_CONFIG["root"] or the
HOMMEY_SKILLS_ROOT environment variable. It defaults to .agents/skills.
"""
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from agentscope.agent import AgentBase
from settings import SKILL_CONFIG

logger = logging.getLogger(__name__)


class LazyAgentRegistry:
    """Discover skill plugins and instantiate their AgentBase subclasses on use."""

    def __init__(
        self,
        model,
        cache: Dict,
        memory_manager=None,
        mcp_manager=None,
        skills_root: Optional[str] = None,
    ):
        self.model = model
        self.cache = cache
        self.memory_manager = memory_manager
        self.mcp_manager = mcp_manager

        project_root = Path(__file__).parent.parent.resolve()
        configured_root = skills_root or SKILL_CONFIG.get("root", ".agents/skills")
        self.skills_root = Path(configured_root)
        if not self.skills_root.is_absolute():
            self.skills_root = project_root / self.skills_root
        self.skills_root = self.skills_root.resolve()

        self._skill_map: Dict[str, Path] = {}
        self._agent_to_skill: Dict[str, str] = {}
        self._discover_skills()

    def _discover_skills(self) -> None:
        if not self.skills_root.exists():
            logger.warning("Skills directory %s not found", self.skills_root)
            return

        from utils.skill_loader import SkillLoader

        loader = SkillLoader(str(self.skills_root))
        for skill_name, definition in loader.load_definitions().items():
            agent_script = self.skills_root / skill_name / definition.entrypoint
            if definition.agent_name and agent_script.exists():
                self._skill_map[skill_name] = agent_script
                self._agent_to_skill[definition.agent_name] = skill_name

    def _resolve_agent_name(self, agent_name: str) -> Optional[str]:
        if agent_name in self._skill_map:
            return agent_name

        skill_name = self._agent_to_skill.get(agent_name)
        if skill_name and skill_name in self._skill_map:
            return skill_name

        return None

    def __getitem__(self, agent_name: str):
        if agent_name in self.cache:
            return self.cache[agent_name]

        skill_name = self._resolve_agent_name(agent_name)
        if not skill_name:
            available = ", ".join(self.keys()) or "none"
            raise KeyError(
                f"Agent '{agent_name}' not found under {self.skills_root}. "
                f"Available agents: {available}"
            )

        script_path = self._skill_map[skill_name]
        logger.info("Loading %s from skill %s", agent_name, skill_name)

        try:
            safe_skill_name = skill_name.replace("-", "_")
            module_name = f"hommey_dynamic_skills.{safe_skill_name}.agent"
            spec = importlib.util.spec_from_file_location(module_name, script_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load spec from {script_path}")

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module

            project_root = str(Path(__file__).parent.parent.resolve())
            if project_root not in sys.path:
                sys.path.insert(0, project_root)

            spec.loader.exec_module(module)

            agent_class = None
            for _, obj in inspect.getmembers(module):
                if inspect.isclass(obj) and issubclass(obj, AgentBase) and obj is not AgentBase:
                    agent_class = obj
                    break

            if not agent_class:
                raise ValueError(f"No AgentBase subclass found in {script_path}")

            init_params = {
                "name": agent_name,
                "model": self.model,
            }

            sig = inspect.signature(agent_class.__init__)
            if "memory_manager" in sig.parameters:
                init_params["memory_manager"] = self.memory_manager
            if "mcp_manager" in sig.parameters:
                init_params["mcp_manager"] = self.mcp_manager
            if "skills_root" in sig.parameters:
                init_params["skills_root"] = str(self.skills_root)

            agent_instance = agent_class(**init_params)
            self.cache[agent_name] = agent_instance
            logger.info("%s loaded", agent_name)
            return agent_instance
        except Exception as exc:
            logger.exception("Failed to load %s: %s", agent_name, exc)
            raise

    def __contains__(self, agent_name: str) -> bool:
        return self._resolve_agent_name(agent_name) is not None or agent_name in self.cache

    def get(self, agent_name: str, default=None):
        try:
            return self[agent_name]
        except KeyError:
            return default

    def keys(self):
        keys = set(self._skill_map.keys())
        keys.update(self._agent_to_skill)
        return sorted(keys)

    def skill_name_for_agent(self, agent_name: str) -> Optional[str]:
        if agent_name in self._skill_map:
            return agent_name
        return self._agent_to_skill.get(agent_name)

    def values(self):
        return self.cache.values()

    def items(self):
        return self.cache.items()

    def get_loaded_agents(self) -> list:
        return list(self.cache.keys())
