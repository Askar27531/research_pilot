import hashlib
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.documents.errors import (
    DocumentNotFoundError,
    DocumentSecurityError,
    DocumentValidationError,
)
from app.schemas import DocumentEntry, WorkspaceManifest

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class WorkspaceManager:
    def __init__(self, root: str | Path, *, max_document_bytes: int = 50_000_000) -> None:
        self.root = Path(root).resolve()
        self.max_document_bytes = max_document_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def project_root(self, project_id: str) -> Path:
        if not SAFE_ID.fullmatch(project_id):
            raise DocumentSecurityError("Invalid project identifier")
        path = (self.root / project_id).resolve()
        self._ensure_within(path, self.root)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def delete_project(self, project_id: str) -> None:
        """Remove one validated project directory without creating it first."""
        if not SAFE_ID.fullmatch(project_id):
            raise DocumentSecurityError("Invalid project identifier")
        path = (self.root / project_id).resolve()
        self._ensure_within(path, self.root)
        if path.is_dir():
            shutil.rmtree(path)

    def resolve_safe_path(self, project_id: str, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise DocumentSecurityError("Absolute paths and path traversal are not allowed")
        project_root = self.project_root(project_id)
        resolved = (project_root / candidate).resolve()
        self._ensure_within(resolved, project_root)
        return resolved

    def import_pdf(self, project_id: str, source: str | Path) -> DocumentEntry:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise DocumentNotFoundError(f"PDF not found: {source_path.name}")
        size = source_path.stat().st_size
        if size <= 0 or size > self.max_document_bytes:
            raise DocumentValidationError("PDF size is outside the configured limit")
        if source_path.suffix.casefold() != ".pdf":
            raise DocumentValidationError("Only .pdf documents are supported")
        with source_path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise DocumentValidationError("File content is not a PDF")
        digest = self._sha256(source_path)
        manifest = self.load_manifest(project_id)
        for entry in manifest.documents:
            if entry.sha256 == digest:
                return entry
        document_id = str(uuid4())
        relative_path = f"papers/{document_id}.pdf"
        destination = self.resolve_safe_path(project_id, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination)
        entry = DocumentEntry(
            document_id=document_id,
            sha256=digest,
            relative_path=relative_path,
            original_name=source_path.name,
            size_bytes=size,
        )
        manifest.documents.append(entry)
        self.save_manifest(manifest)
        return entry

    def import_pdf_bytes(self, project_id: str, filename: str, content: bytes) -> DocumentEntry:
        safe_name = Path(filename).name
        if not safe_name or Path(safe_name).suffix.casefold() != ".pdf":
            raise DocumentValidationError("Only .pdf documents are supported")
        if len(content) <= 0 or len(content) > self.max_document_bytes:
            raise DocumentValidationError("PDF size is outside the configured limit")
        if not content.startswith(b"%PDF-"):
            raise DocumentValidationError("File content is not a PDF")
        digest = hashlib.sha256(content).hexdigest()
        manifest = self.load_manifest(project_id)
        for entry in manifest.documents:
            if entry.sha256 == digest:
                return entry
        document_id = str(uuid4())
        relative_path = f"papers/{document_id}.pdf"
        destination = self.resolve_safe_path(project_id, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        entry = DocumentEntry(
            document_id=document_id,
            sha256=digest,
            relative_path=relative_path,
            original_name=safe_name,
            size_bytes=len(content),
        )
        manifest.documents.append(entry)
        self.save_manifest(manifest)
        return entry

    def get_document(self, project_id: str, document_id: str) -> DocumentEntry:
        if not SAFE_ID.fullmatch(document_id):
            raise DocumentSecurityError("Invalid document identifier")
        manifest = self.load_manifest(project_id)
        for entry in manifest.documents:
            if entry.document_id == document_id:
                path = self.resolve_safe_path(project_id, entry.relative_path)
                if not path.is_file():
                    raise DocumentNotFoundError(f"Document file is missing: {document_id}")
                return entry
        raise DocumentNotFoundError(f"Unknown document: {document_id}")

    def load_manifest(self, project_id: str) -> WorkspaceManifest:
        path = self.resolve_safe_path(project_id, "manifest.json")
        if not path.exists():
            return WorkspaceManifest(
                project_id=project_id,
                generated_at=datetime.now(UTC).isoformat(),
            )
        return WorkspaceManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: WorkspaceManifest) -> None:
        manifest.generated_at = datetime.now(UTC).isoformat()
        path = self.resolve_safe_path(manifest.project_id, "manifest.json")
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _ensure_within(path: Path, root: Path) -> None:
        if not path.is_relative_to(root):
            raise DocumentSecurityError("Resolved path escapes project workspace")
