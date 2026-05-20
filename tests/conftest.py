"""pytest fixtures: 一時 DB を tools.config.KG_DB に差し込む。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import config  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "kg.sqlite"
    monkeypatch.setattr(config, "KG_DB", db_path)
    import db as kg  # late import so monkeypatch is applied
    conn = kg.connect()
    yield conn
    conn.close()
