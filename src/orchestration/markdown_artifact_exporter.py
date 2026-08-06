"""Safe export of a validated ``report.markdown`` Artifact.

The business Report Agent only produces an immutable Artifact.  This module is
the boundary that turns that Artifact into a caller-selected file.  Keeping
the path and run directory here prevents an Agent from deciding where files
are written while still making the exporter useful for demos and other
callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from src.interface.artifact import Artifact, compute_checksum
from src.orchestration.schema_registry import SchemaRegistry, get_schema_registry

REPORT_LOGICAL_NAME = "report.markdown"
REPORT_SCHEMA_REF = "report.markdown@v1"
DEFAULT_REPORT_FILENAME = "final-report.md"
DEFAULT_MANIFEST_FILENAME = "export-manifest.json"

_SAFE_COMPONENT_RE = re.compile(r"[^0-9A-Za-z._-]+")
_EXPORT_LOCKS_GUARD = threading.Lock()
_EXPORT_LOCKS: dict[str, threading.Lock] = {}
_EXPORT_CLAIM_TIMEOUT_SECONDS = 10.0
_EXPORT_CLAIM_POLL_SECONDS = 0.01


class MarkdownArtifactExportError(ValueError):
    """Raised when an Artifact cannot be exported safely."""


def _export_lock(destination_dir: Path) -> threading.Lock:
    """Return the process-local lock that serializes one export directory."""

    key = os.path.normcase(str(destination_dir))
    with _EXPORT_LOCKS_GUARD:
        return _EXPORT_LOCKS.setdefault(key, threading.Lock())


def _claim_path(
    destination_dir: Path,
    report_filename: str,
    manifest_filename: str,
) -> Path:
    identity = hashlib.sha256(
        f"{report_filename}\0{manifest_filename}".encode("utf-8")
    ).hexdigest()[:16]
    return destination_dir / f".markdown-export-{identity}.claim"


def _acquire_export_claim(
    claim_path: Path,
    report_path: Path,
    manifest_path: Path,
) -> bool:
    """Atomically claim an export across processes or wait for its result."""

    deadline = time.monotonic() + _EXPORT_CLAIM_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(
                claim_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            if report_path.exists() and manifest_path.exists():
                return False
            if time.monotonic() >= deadline:
                raise FileExistsError(
                    f"timed out waiting for export claim in {claim_path.parent}"
                )
            time.sleep(_EXPORT_CLAIM_POLL_SECONDS)
            continue
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        return True


def _safe_component(value: str, *, label: str) -> str:
    """Return a filesystem-safe component, rejecting traversal markers."""

    text = str(value)
    if not text or text in {".", ".."}:
        raise MarkdownArtifactExportError(f"{label} must be a non-empty component")
    if "\\" in text or "/" in text or ":" in text:
        raise MarkdownArtifactExportError(f"{label} must not contain path separators")

    cleaned = _SAFE_COMPONENT_RE.sub("_", text).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise MarkdownArtifactExportError(f"{label} is not a safe path component")
    return cleaned


def _safe_relative_dir(relative_dir: str | os.PathLike[str] | None) -> Path:
    """Sanitize a relative directory without allowing an absolute escape."""

    if relative_dir is None or str(relative_dir) in {"", "."}:
        return Path()

    raw = str(relative_dir).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise MarkdownArtifactExportError("relative_dir must be relative")

    components: list[str] = []
    for component in raw.split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            raise MarkdownArtifactExportError("relative_dir must not contain '..'")
        components.append(_safe_component(component, label="relative_dir component"))
    return Path(*components)


def _atomic_write(path: Path, content: bytes) -> None:
    """Write bytes beside ``path`` and atomically replace the destination."""

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _ensure_report_schema(registry: SchemaRegistry) -> None:
    if registry.has(REPORT_SCHEMA_REF):
        return
    # The catalogue is intentionally loaded lazily so this small data-plane
    # utility remains importable without importing the workflow stack.
    from src.contracts.agent_schema_catalog import register_agent_schemas

    register_agent_schemas(registry)


def _validate_report_artifact(
    artifact: Artifact,
    registry: SchemaRegistry,
) -> tuple[dict[str, Any], str]:
    if artifact.logical_name != REPORT_LOGICAL_NAME:
        raise MarkdownArtifactExportError(
            f"only {REPORT_LOGICAL_NAME!r} Artifacts may be exported"
        )
    if artifact.schema_ref != REPORT_SCHEMA_REF:
        raise MarkdownArtifactExportError(
            f"expected schema {REPORT_SCHEMA_REF!r}, got {artifact.schema_ref!r}"
        )
    if artifact.schema_valid is not True:
        raise MarkdownArtifactExportError("Artifact schema_valid must be true")
    if not isinstance(artifact.payload, dict):
        raise MarkdownArtifactExportError("report Artifact payload must be an object")

    _ensure_report_schema(registry)
    valid, errors = registry.validate(artifact.payload, REPORT_SCHEMA_REF)
    if not valid:
        raise MarkdownArtifactExportError(
            "report Artifact failed schema validation: " + "; ".join(errors)
        )

    markdown = artifact.payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise MarkdownArtifactExportError("report Artifact markdown must be non-empty")

    checksum = compute_checksum(artifact.payload)
    if artifact.checksum is not None and artifact.checksum != checksum:
        raise MarkdownArtifactExportError("Artifact checksum does not match payload")
    return artifact.payload, checksum


def _existing_manifest_is_same(
    manifest_path: Path,
    expected: dict[str, Any],
) -> bool:
    if not manifest_path.exists():
        return False
    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return all(current.get(key) == value for key, value in expected.items())


def _existing_export_result(
    *,
    report_path: Path,
    manifest_path: Path,
    expected_manifest: dict[str, Any],
    expected_markdown: bytes,
) -> dict[str, Any] | None:
    if not report_path.exists() and not manifest_path.exists():
        return None
    if (
        report_path.exists()
        and manifest_path.exists()
        and _existing_manifest_is_same(manifest_path, expected_manifest)
        and report_path.read_bytes() == expected_markdown
    ):
        return expected_manifest
    raise FileExistsError(
        f"refusing to overwrite existing export in {report_path.parent}"
    )


def export_markdown_artifact(
    artifact: Artifact,
    output_root: str | os.PathLike[str],
    *,
    relative_dir: str | os.PathLike[str] | None = None,
    filename: str = DEFAULT_REPORT_FILENAME,
    manifest_filename: str = DEFAULT_MANIFEST_FILENAME,
    schema_registry: SchemaRegistry | None = None,
) -> dict[str, Any]:
    """Export one validated report Artifact and write its manifest.

    ``output_root`` and ``relative_dir`` are supplied by the caller.  Existing
    files are treated as immutable: an exact repeat of the same export is
    idempotent, while a different Artifact raises instead of silently
    overwriting another run.
    """

    registry = schema_registry or get_schema_registry()
    payload, checksum = _validate_report_artifact(artifact, registry)
    safe_dir = _safe_relative_dir(relative_dir)
    safe_filename = _safe_component(filename, label="filename")
    safe_manifest_filename = _safe_component(
        manifest_filename, label="manifest_filename"
    )
    # Be conservative across supported filesystems: a demo produced on Linux
    # may later be copied to a case-insensitive Windows volume.
    if safe_filename.casefold() == safe_manifest_filename.casefold():
        raise MarkdownArtifactExportError(
            "filename and manifest_filename must be different"
        )

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    destination_dir = (root / safe_dir).resolve()
    try:
        destination_dir.relative_to(root)
    except ValueError as exc:  # defensive check in addition to component checks
        raise MarkdownArtifactExportError("export path escapes output_root") from exc
    destination_dir.mkdir(parents=True, exist_ok=True)

    report_path = destination_dir / safe_filename
    manifest_path = destination_dir / safe_manifest_filename
    relative_path = report_path.relative_to(root).as_posix()
    manifest = {
        "artifact_id": artifact.artifact_id,
        "version": artifact.version,
        "logical_name": artifact.logical_name,
        "schema_ref": artifact.schema_ref,
        "checksum": checksum,
        "relative_path": relative_path,
    }

    markdown_bytes = payload["markdown"].encode("utf-8")
    claim_path = _claim_path(
        destination_dir,
        safe_filename,
        safe_manifest_filename,
    )

    # The in-process lock avoids needless polling between local threads.  The
    # O_EXCL claim is the actual cross-process authority for the export pair.
    with _export_lock(destination_dir):
        existing = _existing_export_result(
            report_path=report_path,
            manifest_path=manifest_path,
            expected_manifest=manifest,
            expected_markdown=markdown_bytes,
        )
        if existing is not None:
            return existing

        owns_claim = _acquire_export_claim(
            claim_path,
            report_path,
            manifest_path,
        )
        if not owns_claim:
            existing = _existing_export_result(
                report_path=report_path,
                manifest_path=manifest_path,
                expected_manifest=manifest,
                expected_markdown=markdown_bytes,
            )
            if existing is not None:
                return existing
            raise FileExistsError(
                f"refusing to overwrite existing export in {destination_dir}"
            )

        try:
            existing = _existing_export_result(
                report_path=report_path,
                manifest_path=manifest_path,
                expected_manifest=manifest,
                expected_markdown=markdown_bytes,
            )
            if existing is not None:
                return existing
            _atomic_write(report_path, markdown_bytes)
            _atomic_write(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
        except Exception:
            # A report without its manifest is not a valid completed export.
            # Leave it recoverable; a retry refuses to hide the partial state.
            raise
        finally:
            try:
                claim_path.unlink()
            except FileNotFoundError:
                pass
    return manifest


__all__ = [
    "DEFAULT_MANIFEST_FILENAME",
    "DEFAULT_REPORT_FILENAME",
    "MarkdownArtifactExportError",
    "REPORT_LOGICAL_NAME",
    "REPORT_SCHEMA_REF",
    "export_markdown_artifact",
]
