from pathlib import Path

import pytest

from app.skills import SkillNotFoundError, SkillRegistry, SkillSecurityError


def write_skill(root: Path, name: str, body: str = "# Skill\nInstructions") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"name: {name}\ndescription: Test skill\nversion: 1.0.0\n\n{body}",
        encoding="utf-8",
    )
    return path


def test_registry_discovers_metadata_and_loads_content_on_demand(tmp_path) -> None:
    write_skill(tmp_path, "first-skill")
    registry = SkillRegistry(tmp_path)

    metadata = registry.discover()
    loaded = registry.load_skill("first-skill")

    assert [item.name for item in metadata] == ["first-skill"]
    assert not hasattr(metadata[0], "content")
    assert "Instructions" in loaded.content
    assert registry.load_skill("first-skill") is loaded


def test_registry_rejects_unknown_and_oversized_skills(tmp_path) -> None:
    write_skill(tmp_path, "large-skill", "x" * 2_000)
    registry = SkillRegistry(tmp_path, max_bytes=1_000)
    registry.discover()

    with pytest.raises(SkillNotFoundError):
        registry.load_skill("../unknown")
    with pytest.raises(SkillSecurityError):
        registry.load_skill("large-skill")


def test_registry_rejects_path_escape_even_for_tampered_metadata(tmp_path) -> None:
    outside = tmp_path.parent / "outside-skill.md"
    outside.write_text("secret", encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    registry.discover()
    from app.schemas import SkillMetadata

    registry._metadata["escaped"] = SkillMetadata(
        name="escaped",
        description="tampered",
        version="1",
        path=str(outside),
    )
    with pytest.raises(SkillSecurityError):
        registry.load_skill("escaped")
