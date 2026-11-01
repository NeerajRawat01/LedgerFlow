"""Tests for filesystem storage path resolution."""

from __future__ import annotations

from pathlib import Path

from app.storage.filesystem import FileStoragePaths


def test_default_layout(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    output = tmp_path / "output"
    data = tmp_path / "data"

    paths = FileStoragePaths(data_dir=data, storage_dir=storage, output_dir=output)
    paths.ensure_layout()

    assert paths.runs_dir.is_dir()
    assert paths.issues_dir.is_dir()
    assert paths.agent_logs_dir.is_dir()
    assert output.is_dir()
    # Source data is never created by the application.
    assert not data.exists()


def test_source_data_dir_points_at_immutable_inputs(tmp_path: Path) -> None:
    data = tmp_path / "data"
    paths = FileStoragePaths(
        data_dir=data,
        storage_dir=tmp_path / "storage",
        output_dir=tmp_path / "output",
    )

    assert paths.source_data_dir == data
