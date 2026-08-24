from pathlib import Path

from app.schemas import LoadedSkill, SkillMetadata


class SkillRegistryError(ValueError):
    pass


class SkillNotFoundError(SkillRegistryError):
    pass


class SkillSecurityError(SkillRegistryError):
    pass


class SkillRegistry:
    """Discover skill metadata eagerly and load allow-listed content on demand."""

    def __init__(self, root: str | Path, *, max_bytes: int = 64_000) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self._metadata: dict[str, SkillMetadata] = {}
        self._cache: dict[str, LoadedSkill] = {}

    def discover(self) -> list[SkillMetadata]:
        self._metadata = {}
        if not self.root.exists():
            return []
        for path in sorted(self.root.glob("*/SKILL.md")):
            resolved = path.resolve()
            self._ensure_within_root(resolved)
            fields = self._read_header(resolved)
            metadata = SkillMetadata(
                name=fields["name"],
                description=fields["description"],
                version=fields["version"],
                path=str(resolved.relative_to(self.root)),
            )
            if metadata.name in self._metadata:
                raise SkillRegistryError(f"Duplicate skill name: {metadata.name}")
            self._metadata[metadata.name] = metadata
        return self.list()

    def list(self) -> list[SkillMetadata]:
        return [self._metadata[name] for name in sorted(self._metadata)]

    def load_skill(self, name: str) -> LoadedSkill:
        if name in self._cache:
            return self._cache[name]
        metadata = self._metadata.get(name)
        if metadata is None:
            raise SkillNotFoundError(f"Unknown skill: {name}")
        path = (self.root / metadata.path).resolve()
        self._ensure_within_root(path)
        size = path.stat().st_size
        if size > self.max_bytes:
            raise SkillSecurityError(f"Skill exceeds {self.max_bytes} bytes: {name}")
        loaded = LoadedSkill(**metadata.model_dump(), content=path.read_text(encoding="utf-8"))
        self._cache[name] = loaded
        return loaded

    def _ensure_within_root(self, path: Path) -> None:
        if not path.is_relative_to(self.root):
            raise SkillSecurityError("Skill path escapes configured root")

    @staticmethod
    def _read_header(path: Path) -> dict[str, str]:
        fields: dict[str, str] = {}
        with path.open("r", encoding="utf-8") as stream:
            for _ in range(20):
                line = stream.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped.startswith("# "):
                    break
                if ":" in stripped:
                    key, value = stripped.split(":", 1)
                    if key in {"name", "description", "version"}:
                        fields[key] = value.strip()
        missing = {"name", "description", "version"} - fields.keys()
        if missing:
            raise SkillRegistryError(f"Missing skill metadata {sorted(missing)} in {path.name}")
        return fields
