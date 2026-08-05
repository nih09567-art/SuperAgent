from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.interface.artifact import Artifact, compute_checksum
from src.orchestration.markdown_artifact_exporter import (
    MarkdownArtifactExportError,
    export_markdown_artifact,
)


def _report(*, markdown: str = "# 王强\n\n20年工龄，15天年假。", **overrides) -> Artifact:
    payload = {
        "title": "annual leave",
        "markdown": markdown,
        "source_count": 2,
    }
    payload.update(overrides.pop("payload", {}))
    logical_name = overrides.pop("logical_name", "report.markdown")
    schema_ref = overrides.pop("schema_ref", "report.markdown@v1")
    schema_valid = overrides.pop("schema_valid", True)
    artifact = Artifact(
        artifact_id="report-artifact-1",
        version=1,
        logical_name=logical_name,
        schema_ref=schema_ref,
        payload=payload,
        schema_valid=schema_valid,
        **overrides,
    )
    return artifact.with_checksum()


def test_exports_utf8_content_and_manifest(tmp_path):
    artifact = _report()

    manifest = export_markdown_artifact(
        artifact,
        tmp_path,
        relative_dir="annual-leave/run-1",
    )

    report_path = tmp_path / "annual-leave" / "run-1" / "final-report.md"
    manifest_path = report_path.with_name("export-manifest.json")
    assert report_path.read_text(encoding="utf-8") == artifact.payload["markdown"]
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest["artifact_id"] == artifact.artifact_id
    assert manifest["checksum"] == compute_checksum(artifact.payload)
    assert manifest["relative_path"] == "annual-leave/run-1/final-report.md"


def test_export_is_idempotent_for_same_artifact(tmp_path):
    artifact = _report()
    first = export_markdown_artifact(artifact, tmp_path, relative_dir="run-1")
    second = export_markdown_artifact(artifact, tmp_path, relative_dir="run-1")
    assert second == first


def test_export_refuses_to_overwrite_another_artifact(tmp_path):
    export_markdown_artifact(_report(), tmp_path, relative_dir="run-1")
    other = _report(markdown="# different")
    other.artifact_id = "another-artifact"
    other = other.with_checksum()

    with pytest.raises(FileExistsError):
        export_markdown_artifact(other, tmp_path, relative_dir="run-1")


@pytest.mark.parametrize(
    "relative_dir",
    ["../outside", "annual-leave/../../outside", "C:/outside", "/outside"],
)
def test_export_rejects_path_traversal(tmp_path, relative_dir):
    with pytest.raises(MarkdownArtifactExportError):
        export_markdown_artifact(_report(), tmp_path, relative_dir=relative_dir)


def test_export_rejects_path_separator_in_filename(tmp_path):
    with pytest.raises(MarkdownArtifactExportError):
        export_markdown_artifact(_report(), tmp_path, filename="../outside.md")


@pytest.mark.parametrize(
    ("filename", "manifest_filename"),
    [
        ("same name.md", "same_name.md"),
        ("Report.md", "report.md"),
    ],
)
def test_export_rejects_report_manifest_filename_collision(
    tmp_path, filename, manifest_filename
):
    with pytest.raises(
        MarkdownArtifactExportError,
        match="filename and manifest_filename must be different",
    ):
        export_markdown_artifact(
            _report(),
            tmp_path,
            filename=filename,
            manifest_filename=manifest_filename,
        )
    assert list(tmp_path.iterdir()) == []


def test_concurrent_different_artifacts_cannot_both_succeed(
    tmp_path, monkeypatch
):
    from src.orchestration import markdown_artifact_exporter as exporter

    original_atomic_write = exporter._atomic_write
    start = Barrier(2)

    def slow_atomic_write(path, content):
        if path.name == "final-report.md":
            time.sleep(0.05)
        original_atomic_write(path, content)

    monkeypatch.setattr(exporter, "_atomic_write", slow_atomic_write)
    first = _report(markdown="# first")
    second = _report(markdown="# second")
    second.artifact_id = "report-artifact-2"
    second = second.with_checksum()

    def export(artifact):
        start.wait(timeout=5)
        return export_markdown_artifact(artifact, tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(export, artifact) for artifact in (first, second)]

    successes = []
    failures = []
    for future in futures:
        try:
            successes.append(future.result())
        except Exception as exc:  # both outcomes are asserted below
            failures.append(exc)

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FileExistsError)
    manifest = json.loads(
        (tmp_path / "export-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == successes[0]
    expected_markdown = (
        first.payload["markdown"]
        if manifest["artifact_id"] == first.artifact_id
        else second.payload["markdown"]
    )
    assert (
        tmp_path / "final-report.md"
    ).read_text(encoding="utf-8") == expected_markdown


@pytest.mark.parametrize(
    "artifact",
    [
        _report(logical_name="other"),
        _report(schema_ref="report.markdown@v9"),
        _report(schema_valid=False),
        _report(payload={"title": "x", "markdown": "# x", "source_count": "2"}),
        _report(markdown="  \n\t"),
    ],
)
def test_export_requires_valid_report_artifact(tmp_path, artifact):
    with pytest.raises(MarkdownArtifactExportError):
        export_markdown_artifact(artifact, tmp_path)


def test_export_does_not_mutate_artifact(tmp_path):
    artifact = _report(checksum=None)
    before = artifact.model_dump(mode="json")
    export_markdown_artifact(artifact, tmp_path)
    assert artifact.model_dump(mode="json") == before


def test_export_rejects_checksum_mismatch(tmp_path):
    artifact = _report()
    artifact.checksum = "not-the-payload-checksum"
    with pytest.raises(MarkdownArtifactExportError):
        export_markdown_artifact(artifact, tmp_path)
