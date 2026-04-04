"""Skill loader — reads YAML skill definitions and makes them available to agents and slash commands."""

import logging
from pathlib import Path

import yaml

from src.core.base import Skill, SkillParam

logger = logging.getLogger(__name__)

# Global skill registry
_skill_registry: dict[str, Skill] = {}


def load_skills(skills_dir: str | Path = "skills") -> dict[str, Skill]:
    """Load all skill YAML files from the skills directory."""
    skills_path = Path(skills_dir)
    if not skills_path.exists():
        logger.info(f"Skills directory {skills_path} does not exist, skipping")
        return {}

    loaded = 0
    for yaml_file in sorted(skills_path.glob("*.yaml")):
        try:
            skill = _load_skill_file(yaml_file)
            _skill_registry[skill.name] = skill
            loaded += 1
        except Exception as e:
            logger.error(f"Failed to load skill {yaml_file}: {e}")

    # Also check for agent-specific skills in agents/*/skills/
    agents_path = Path("agents")
    if agents_path.exists():
        for agent_skills_dir in agents_path.glob("*/skills"):
            for yaml_file in sorted(agent_skills_dir.glob("*.yaml")):
                try:
                    skill = _load_skill_file(yaml_file)
                    # Prefix with agent name to avoid collisions
                    agent_name = agent_skills_dir.parent.name
                    skill.name = f"{agent_name}:{skill.name}"
                    _skill_registry[skill.name] = skill
                    loaded += 1
                except Exception as e:
                    logger.error(f"Failed to load skill {yaml_file}: {e}")

    logger.info(f"Loaded {loaded} skills")
    return dict(_skill_registry)


def _load_skill_file(path: Path) -> Skill:
    """Parse a single skill YAML file into a Skill object."""
    with open(path) as f:
        data = yaml.safe_load(f)

    if not data or "name" not in data:
        raise ValueError(f"Skill file {path} missing 'name' field")

    params = []
    if "parameters" in data:
        for param_name, param_def in data["parameters"].items():
            if isinstance(param_def, str):
                # Shorthand: just a type
                params.append(SkillParam(name=param_name, type=param_def))
            elif isinstance(param_def, dict):
                params.append(SkillParam(
                    name=param_name,
                    type=param_def.get("type", "string"),
                    description=param_def.get("description", ""),
                    required=param_def.get("required", False),
                    choices=param_def.get("choices"),
                ))

    return Skill(
        name=data["name"],
        description=data.get("description", ""),
        prompt=data.get("prompt", ""),
        tools=data.get("tools", []),
        parameters=params,
    )


def get_skill(name: str) -> Skill | None:
    """Get a registered skill by name."""
    return _skill_registry.get(name)


def get_all_skills() -> dict[str, Skill]:
    """Get all registered skills."""
    return dict(_skill_registry)


def render_skill_prompt(skill: Skill, params: dict[str, str]) -> str:
    """Render a skill's prompt template with the given parameters."""
    prompt = skill.prompt
    for key, value in params.items():
        prompt = prompt.replace(f"{{{key}}}", str(value))
    return prompt
