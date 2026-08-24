import csv
import hashlib
import re
from io import StringIO
from uuid import uuid4

from app.db import ArtifactRepository
from app.documents import DocumentNotFoundError, DocumentValidationError, WorkspaceManager
from app.schemas import ArtifactRecord

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
MERMAID_STARTS = (
    "graph ",
    "flowchart ",
    "sequenceDiagram",
    "stateDiagram",
    "classDiagram",
    "erDiagram",
)


class ArtifactService:
    def __init__(self, workspace: WorkspaceManager, repository: ArtifactRepository) -> None:
        self.workspace = workspace
        self.repository = repository

    async def markdown(
        self, project_id: str, name: str, title: str, sections: dict[str, str]
    ) -> ArtifactRecord:
        content = (
            f"# {title}\n\n"
            + "\n\n".join(f"## {heading}\n\n{body}" for heading, body in sections.items())
            + "\n"
        )
        return await self._store(project_id, name, "markdown", "md", content.encode("utf-8"))

    async def csv(
        self, project_id: str, name: str, columns: list[str], rows: list[list[str]]
    ) -> ArtifactRecord:
        if any(len(row) != len(columns) for row in rows):
            raise DocumentValidationError("Every CSV row must match the column count")
        stream = StringIO()
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return await self._store(
            project_id, name, "csv", "csv", stream.getvalue().encode("utf-8-sig")
        )

    async def mermaid(self, project_id: str, name: str, diagram: str) -> ArtifactRecord:
        stripped = diagram.strip()
        if not stripped.startswith(MERMAID_STARTS):
            raise DocumentValidationError("Unsupported Mermaid diagram declaration")
        lowered = stripped.casefold()
        if any(token in lowered for token in ("<script", "javascript:", "%%{init", "click ")):
            raise DocumentValidationError("Unsafe Mermaid directive")
        return await self._store(
            project_id, name, "mermaid", "mmd", (stripped + "\n").encode("utf-8")
        )

    async def read(self, record: ArtifactRecord) -> bytes:
        path = self.workspace.resolve_safe_path(record.project_id, record.relative_path)
        if not path.is_file():
            raise DocumentNotFoundError("Artifact file is missing")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != record.sha256:
            raise DocumentValidationError("Artifact hash does not match repository metadata")
        return content

    async def _store(
        self, project_id: str, name: str, artifact_type: str, extension: str, content: bytes
    ) -> ArtifactRecord:
        if not SAFE_NAME.fullmatch(name):
            raise DocumentValidationError("Artifact name contains unsafe characters")
        artifact_id = str(uuid4())
        relative = f"artifacts/{artifact_id}.{extension}"
        path = self.workspace.resolve_safe_path(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        try:
            return await self.repository.create(
                project_id,
                artifact_type,
                name,
                relative,
                hashlib.sha256(content).hexdigest(),
                len(content),
                artifact_id,
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
