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


@pytest.fixture
def fake_embed_cached(monkeypatch):
    """llm.embed_cached を辞書ベースの偽実装に置換 (Ollama 不要)。

    Usage:
        def test_x(fake_embed_cached):
            fake_embed_cached({"Anthropic": [1.0, 0.0], "Anthropic, PBC": [0.95, 0.05]})
            # 以降 llm.embed_cached("Anthropic") -> [1.0, 0.0]
            # 辞書に無い key は None (Ollama 失敗時の縮退と同等)
    """
    import llm

    def _install(vectors: dict[str, list[float]] | None = None):
        vec_map = dict(vectors or {})

        def fake(text: str):
            return vec_map.get(text)

        monkeypatch.setattr(llm, "embed_cached", fake)

    return _install
