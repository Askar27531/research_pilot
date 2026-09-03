"""Skill-integrity guard.

Keeps the runtime skill registry and the code that consumes skills in lock-step:
- every registered skill (skills/<name>/SKILL.md) must be referenced by a
  ``load_skill("name")`` call or by ``SKILL_FOR_RESPONSE_MODEL``; and
- every such reference must resolve to a registered skill.

This prevents both dead skills (registered but never loaded) and dangling
references (loading a skill that no longer exists), which silently eroded the
project when the post-analysis workflow stage was retired.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.skills import SkillRegistry
from app.skills.bindings import SKILL_FOR_RESPONSE_MODEL

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
SKILLS_ROOT = REPO_ROOT / "skills"

_LOAD_SKILL_CALL = re.compile(r'load_skill\(\s*["\']([a-z0-9]+(?:-[a-z0-9]+)*)["\']\s*\)')


def _registry() -> SkillRegistry:
    return SkillRegistry(SKILLS_ROOT)


def _referenced_skill_names() -> set[str]:
    referenced = set(SKILL_FOR_RESPONSE_MODEL.values())
    for path in APP_ROOT.rglob("*.py"):
        referenced.update(_LOAD_SKILL_CALL.findall(path.read_text(encoding="utf-8")))
    return referenced


def test_every_registered_skill_is_referenced_by_runtime_code() -> None:
    registered = {skill.name for skill in _registry().discover()}
    orphaned = registered - _referenced_skill_names()
    assert not orphaned, f"skills registered but never referenced by code: {sorted(orphaned)}"


def test_every_skill_reference_resolves_to_a_registered_skill() -> None:
    registered = {skill.name for skill in _registry().discover()}
    dangling = _referenced_skill_names() - registered
    assert not dangling, f"code references skills that are not registered: {sorted(dangling)}"


def test_registered_and_referenced_skill_sets_match() -> None:
    registered = {skill.name for skill in _registry().discover()}
    assert registered == _referenced_skill_names()


def test_every_registered_skill_loads() -> None:
    registry = _registry()
    for metadata in registry.discover():
        loaded = registry.load_skill(metadata.name)
        assert loaded.content.strip(), f"skill {metadata.name} has empty content"
