"""Hidden regression tests, from timu 3d78d04 tests/test_review_fixes.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from timu.workflow import check_files


@pytest.mark.parametrize("command", ["python check.py", "sh check.py"])
def test_a_script_named_without_a_slash_counts(tmp_path: Path, command: str) -> None:
    (tmp_path / "check.py").write_text("")
    workdir = Path(os.path.realpath(tmp_path))
    assert check_files([command], workdir) == ("check.py",)


def test_cargo_and_go_have_check_files(tmp_path: Path) -> None:
    assert "Cargo.toml" in check_files(["cargo test"], tmp_path)
    assert "go.mod" in check_files(["go test ./..."], tmp_path)
