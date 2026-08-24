import pytest

from app.documents import DocumentSecurityError, DocumentValidationError, WorkspaceManager


def test_workspace_import_is_deduplicated_and_manifest_is_persistent(tmp_path) -> None:
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.4\nminimal")
    manager = WorkspaceManager(tmp_path / "workspaces")

    first = manager.import_pdf("project-1", source)
    second = manager.import_pdf("project-1", source)
    manifest = manager.load_manifest("project-1")

    assert first == second
    assert len(manifest.documents) == 1
    assert manager.resolve_safe_path("project-1", first.relative_path).is_file()
    assert not first.relative_path.startswith(str(tmp_path))


@pytest.mark.parametrize("relative", ["../secret.pdf", "papers/../../secret.pdf", "C:/secret.pdf"])
def test_workspace_rejects_path_traversal(tmp_path, relative) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    with pytest.raises(DocumentSecurityError):
        manager.resolve_safe_path("project-1", relative)


def test_workspace_rejects_wrong_extension_magic_and_oversized_file(tmp_path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces", max_document_bytes=10)
    wrong_extension = tmp_path / "paper.txt"
    wrong_extension.write_bytes(b"%PDF-1.4")
    wrong_magic = tmp_path / "paper.pdf"
    wrong_magic.write_bytes(b"not-pdf")
    oversized = tmp_path / "large.pdf"
    oversized.write_bytes(b"%PDF-" + b"x" * 20)

    for source in (wrong_extension, wrong_magic, oversized):
        with pytest.raises(DocumentValidationError):
            manager.import_pdf("project-1", source)


def test_workspace_rejects_symlink_escape(tmp_path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    project = manager.project_root("project-1")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = project / "escaped"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DocumentSecurityError):
        manager.resolve_safe_path("project-1", "escaped/secret.pdf")
