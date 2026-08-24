import hashlib

from app.documents import DocumentNotFoundError, DocumentService, DocumentValidationError
from app.schemas import EvidenceNode, EvidenceSourcePreview


class EvidenceVerifier:
    def __init__(self, documents: DocumentService) -> None:
        self.documents = documents

    def verify(self, node: EvidenceNode) -> EvidenceSourcePreview:
        if node.evidence_type == "text":
            page = self.documents.get_page(node.project_id, node.document_id, node.page_number)
            if node.span_start is None or node.span_end is None or node.excerpt is None:
                raise DocumentValidationError("Text evidence locator is incomplete")
            current = page.text[node.span_start : node.span_end]
            digest = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if current != node.excerpt or digest != node.source_hash:
                raise DocumentValidationError("Text evidence no longer matches parsed source")
            source_path = page.screenshot_path or node.source_path
            self._require_file(node.project_id, source_path)
            return EvidenceSourcePreview(
                evidence=node,
                content_type="text",
                excerpt=current,
                source_path=source_path,
            )
        if node.evidence_type == "figure":
            path = self._require_file(node.project_id, node.source_path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != node.source_hash:
                raise DocumentValidationError("Figure evidence artifact hash has changed")
            return EvidenceSourcePreview(
                evidence=node,
                content_type="image",
                excerpt=node.excerpt,
                source_path=node.source_path,
            )
        parsed = self.documents.get_structure(node.project_id, node.document_id)
        table = next(
            (
                item
                for item in parsed.tables
                if item.page_number == node.page_number and item.label == node.label
            ),
            None,
        )
        if table is None:
            raise DocumentNotFoundError("Table evidence locator no longer exists")
        caption = table.caption or table.label or table.table_id
        if hashlib.sha256(caption.encode("utf-8")).hexdigest() != node.source_hash:
            raise DocumentValidationError("Table evidence caption hash has changed")
        self._require_file(node.project_id, node.source_path)
        return EvidenceSourcePreview(
            evidence=node,
            content_type="image",
            excerpt=node.excerpt,
            source_path=node.source_path,
        )

    def _require_file(self, project_id: str, relative_path: str):
        path = self.documents.workspace.resolve_safe_path(project_id, relative_path)
        if not path.is_file():
            raise DocumentNotFoundError("Evidence source artifact is missing")
        return path
