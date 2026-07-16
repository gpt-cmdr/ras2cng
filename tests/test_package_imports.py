from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def test_webgis_cli_import_does_not_require_analytics_extras() -> None:
    repository = Path(__file__).resolve().parents[1]
    script = r"""
import builtins
import sys

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.partition(".")[0]
    if root in {"duckdb", "sqlalchemy", "geoalchemy2", "psycopg2"}:
        error = ModuleNotFoundError(f"blocked optional dependency: {root}")
        error.name = root
        raise error
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import ras2cng
from ras2cng.cli import app

assert app is not None
assert "duckdb" not in sys.modules
assert "sqlalchemy" not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository)
    subprocess.run([sys.executable, "-c", script], check=True, env=environment)
