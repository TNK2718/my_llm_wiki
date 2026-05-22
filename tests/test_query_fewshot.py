"""query.select_fewshots / _format_fewshots / text2sql.txt 注入の単体テスト。

embedding 動的 Fewshot 選択ロジックを Ollama 不在でも検証するため、
query._embed_cached を monkeypatch で決定論的ベクトルに差し替える。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import config  # noqa: E402
import query  # noqa: E402


@pytest.fixture
def fewshot_pool(tmp_path, monkeypatch):
    """一時 yaml を作って config.FEWSHOT_POOL に差し込み、_load_fewshot_pool の cache も飛ばす。

    Usage:
        fewshot_pool([
            {"id": "a", "q": "foo", "sql": "SELECT 1"},
            ...
        ])
    """
    import yaml

    def _install(items):
        path = tmp_path / "fewshot.yml"
        if items is None:
            # 不在ケース
            monkeypatch.setattr(config, "FEWSHOT_POOL", tmp_path / "missing.yml")
        else:
            path.write_text(
                yaml.safe_dump({"version": 1, "items": items}, allow_unicode=True),
                encoding="utf-8",
            )
            monkeypatch.setattr(config, "FEWSHOT_POOL", path)
        query._load_fewshot_pool.cache_clear()

    yield _install
    query._load_fewshot_pool.cache_clear()


@pytest.fixture
def fake_query_embed(monkeypatch):
    """query._embed_cached を辞書ベースの偽実装に置換。

    辞書に無い key は None を返す (= Ollama 失敗と同等の縮退)。
    """
    def _install(vectors: dict[str, list[float]] | None = None):
        vec_map = dict(vectors or {})

        def fake(text: str):
            return vec_map.get(text)

        monkeypatch.setattr(query, "_embed_cached", fake)

    return _install


def test_select_fewshots_ranks_by_cosine(fewshot_pool, fake_query_embed):
    fewshot_pool([
        {"id": "a", "q": "qa", "sql": "SELECT 'a'"},
        {"id": "b", "q": "qb", "sql": "SELECT 'b'"},
        {"id": "c", "q": "qc", "sql": "SELECT 'c'"},
    ])
    # question と qb がベクトル一致、qc が次点、qa が遠い
    fake_query_embed({
        "question": [1.0, 0.0],
        "qa":       [0.0, 1.0],
        "qb":       [1.0, 0.0],
        "qc":       [0.7, 0.7],
    })
    picks = query.select_fewshots("question", k=2)
    assert [p["id"] for p in picks] == ["b", "c"]


def test_select_fewshots_stable_on_ties(fewshot_pool, fake_query_embed):
    fewshot_pool([
        {"id": "a", "q": "qa", "sql": "SELECT 'a'"},
        {"id": "b", "q": "qb", "sql": "SELECT 'b'"},
        {"id": "c", "q": "qc", "sql": "SELECT 'c'"},
    ])
    # 3 つとも完全に同じ cos-sim → YAML 出現順を維持する
    fake_query_embed({
        "question": [1.0, 0.0],
        "qa": [1.0, 0.0],
        "qb": [1.0, 0.0],
        "qc": [1.0, 0.0],
    })
    picks = query.select_fewshots("question", k=2)
    assert [p["id"] for p in picks] == ["a", "b"]


def test_select_fewshots_empty_pool_returns_empty(fewshot_pool, fake_query_embed):
    fewshot_pool([])
    fake_query_embed({"question": [1.0, 0.0]})
    assert query.select_fewshots("question", k=3) == []


def test_select_fewshots_missing_pool_returns_empty(fewshot_pool, fake_query_embed):
    fewshot_pool(None)  # ファイル不在
    fake_query_embed({"question": [1.0, 0.0]})
    assert query.select_fewshots("question", k=3) == []


def test_select_fewshots_embed_failure_falls_back_to_head(fewshot_pool, fake_query_embed):
    fewshot_pool([
        {"id": "first",  "q": "q1", "sql": "SELECT 1"},
        {"id": "second", "q": "q2", "sql": "SELECT 2"},
        {"id": "third",  "q": "q3", "sql": "SELECT 3"},
    ])
    # question の embed が None → 先頭 k 件決定論的
    fake_query_embed({})
    picks = query.select_fewshots("anything", k=2)
    assert [p["id"] for p in picks] == ["first", "second"]


def test_select_fewshots_skips_items_without_embedding(fewshot_pool, fake_query_embed):
    fewshot_pool([
        {"id": "a", "q": "qa", "sql": "SELECT 'a'"},
        {"id": "b", "q": "qb", "sql": "SELECT 'b'"},
    ])
    # qa は embed 不能 → スキップされ b のみ返る
    fake_query_embed({
        "question": [1.0, 0.0],
        "qb": [0.9, 0.1],
    })
    picks = query.select_fewshots("question", k=3)
    assert [p["id"] for p in picks] == ["b"]


def test_select_fewshots_skips_invalid_items(fewshot_pool, fake_query_embed):
    fewshot_pool([
        {"id": "a", "q": "qa", "sql": "SELECT 'a'"},
        {"id": "no-sql", "q": "qz"},                  # sql 欠落 → スキップ
        {"q": "qb", "sql": "SELECT 'b'"},              # id 欠落 → 自動採番
    ])
    fake_query_embed({
        "question": [1.0, 0.0],
        "qa": [1.0, 0.0],
        "qb": [1.0, 0.0],
    })
    picks = query.select_fewshots("question", k=5)
    ids = [p["id"] for p in picks]
    assert "a" in ids
    assert "no-sql" not in ids
    assert len(ids) == 2  # a + anon


def test_format_fewshots_empty_returns_empty():
    assert query._format_fewshots([]) == ""


def test_format_fewshots_uses_1_indexed_examples():
    block = query._format_fewshots([
        {"id": "x", "q": "質問X", "sql": "SELECT 'x'"},
        {"id": "y", "q": "質問Y", "sql": "SELECT 'y'"},
    ])
    assert "例 1) 質問: 「質問X」" in block
    assert "SQL: SELECT 'x'" in block
    assert "例 2) 質問: 「質問Y」" in block
    assert "SQL: SELECT 'y'" in block
    # ブロック末尾は改行 1 個 (prompt 連結時の余分な空行回避)
    assert block.endswith("\n")
    assert not block.endswith("\n\n")


def test_prompt_template_has_fewshots_placeholder():
    """text2sql.txt が {FEWSHOTS} placeholder を持ち、旧固定例が消えていることを確認。"""
    tmpl = (config.PROMPTS / "text2sql.txt").read_text(encoding="utf-8")
    assert "{FEWSHOTS}" in tmpl
    assert "{HINTS}" in tmpl
    assert "{QUESTION}" in tmpl
    # 旧 hard-coded 例の sentinel は消えていること (回帰防止)
    assert "Acme の CEO は誰か" not in tmpl
    assert "1990 年以降に生まれた person を列挙" not in tmpl


def test_curated_pool_loads_with_expected_seed_ids():
    """data/fewshot/text2sql.yml の初期 seed が読み込めることを確認。"""
    query._load_fewshot_pool.cache_clear()
    pool = query._load_fewshot_pool()
    ids = {it["id"] for it in pool}
    assert {"emp-ceo", "person-range", "claims-history"}.issubset(ids)
