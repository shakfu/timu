"""Package-wide constraints."""

from __future__ import annotations

import ast
from importlib.metadata import requires
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "timu"


def test_has_no_runtime_dependencies() -> None:
    """The distribution must not declare runtime dependencies."""
    assert not (requires("timu") or [])


def test_no_global_rebinding() -> None:
    """Agents may run side by side, so no function rebinds module state (design 9).
    This catches `global`/`nonlocal`, not mutation of module-level containers."""
    offenders = [
        f"{path.relative_to(SRC)}:{node.lineno}"
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(), str(path)))
        if isinstance(node, ast.Global | ast.Nonlocal)
    ]
    assert offenders == []
