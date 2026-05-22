"""schema_block.build_schema_block / select_extra_tables / _format_table_decl の単体テスト。

- DB introspection (PRAGMA table_info) は tmp_db fixture (conftest.py) を流用。
  → 実 schema.sql から CREATE TABLE して migration 適用後の状態を読む。
- embedding 動的選択ロジックは query._embed_cached を monkeypatch で差し替える
  (test_query_fewshot.py と同方針)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import config  # noqa: E402
import query  # noqa: E402
import schema_block  # noqa: E402


@pytest.fixture
def schema_docs(tmp_path, monkeypatch):
    """一時 yaml を作って config.SCHEMA_DOCS に差し込み、_load_schema_docs の cache も飛ばす。"""
    import yaml

    def _install(items):
        if items is None:
            monkeypatch.setattr(config, "SCHEMA_DOCS", tmp_path / "missing.yml")
        else:
            path = tmp_path / "schema_docs.yml"
            path.write_text(
                yaml.safe_dump({"version": 1, "items": items}, allow_unicode=True),
                encoding="utf-8",
            )
            monkeypatch.setattr(config, "SCHEMA_DOCS", path)
        schema_block._load_schema_docs.cache_clear()

    yield _install
    schema_block._load_schema_docs.cache_clear()


@pytest.fixture
def fake_query_embed(monkeypatch):
    """query._embed_cached を辞書ベースの偽実装に置換。

    schema_block.select_extra_tables は `from query import _embed_cached` を関数内で
    遅延 import しているので、query 側の属性を差し替えれば反映される。
    """
    def _install(vectors: dict[str, list[float]] | None = None):
        vec_map = dict(vectors or {})

        def fake(text: str):
            return vec_map.get(text)

        monkeypatch.setattr(query, "_embed_cached", fake)

    return _install


# -----------------------------------------------------------------
# _format_table_decl
# -----------------------------------------------------------------

def test_format_table_decl_returns_name_and_types(tmp_db):
    decl = schema_block._format_table_decl(tmp_db, "person")
    assert decl.startswith("person(")
    assert decl.endswith(")")
    # 列名と型がペアで並ぶ
    assert "id INTEGER" in decl
    assert "canonical_name TEXT" in decl
    assert "norm_key TEXT" in decl


def test_format_table_decl_missing_table_returns_empty(tmp_db):
    assert schema_block._format_table_decl(tmp_db, "no_such_table") == ""


def test_format_table_decl_product_includes_typed_columns(tmp_db):
    """回帰防止: product canonical に typed 列 4 つが並ぶこと (ae4aa01/0bd326a で追加)。"""
    decl = schema_block._format_table_decl(tmp_db, "product")
    assert "billing_period TEXT" in decl
    assert "included_quota_units REAL" in decl
    assert "quota_unit_name TEXT" in decl
    assert "trial_period_days INTEGER" in decl


# -----------------------------------------------------------------
# _load_schema_docs
# -----------------------------------------------------------------

def test_load_schema_docs_missing_file_returns_empty(schema_docs):
    schema_docs(None)
    assert schema_block._load_schema_docs() == ()


def test_load_schema_docs_wrong_version_returns_empty(tmp_path, monkeypatch):
    import yaml
    path = tmp_path / "schema_docs.yml"
    path.write_text(yaml.safe_dump({"version": 2, "items": []}), encoding="utf-8")
    monkeypatch.setattr(config, "SCHEMA_DOCS", path)
    schema_block._load_schema_docs.cache_clear()
    try:
        assert schema_block._load_schema_docs() == ()
    finally:
        schema_block._load_schema_docs.cache_clear()


def test_load_schema_docs_skips_invalid_items(schema_docs):
    schema_docs([
        {"table": "ok_table", "role": "ok role"},
        {"table": "no_role"},               # role 欠落 → スキップ
        {"role": "no table"},               # table 欠落 → スキップ
        "not_a_dict",                       # dict でない → スキップ
    ])
    docs = schema_block._load_schema_docs()
    assert [d["table"] for d in docs] == ["ok_table"]


def test_load_schema_docs_curated_seed_loads():
    """data/schema_docs.yml の初期 seed が読み込めることを確認 (毎リリース壊さない gate)。"""
    schema_block._load_schema_docs.cache_clear()
    docs = schema_block._load_schema_docs()
    tables = {d["table"] for d in docs}
    # 代表 extras がきちんと書かれている
    assert {"person_aliases", "product_claims", "weak_relations", "doc_fts"}.issubset(tables)


# -----------------------------------------------------------------
# select_extra_tables
# -----------------------------------------------------------------

def test_select_extra_tables_ranks_by_cosine(schema_docs, fake_query_embed):
    schema_docs([
        {"table": "a_tbl", "role": "role A"},
        {"table": "b_tbl", "role": "role B"},
        {"table": "c_tbl", "role": "role C"},
    ])
    fake_query_embed({
        "question": [1.0, 0.0],
        "role A":   [0.0, 1.0],
        "role B":   [1.0, 0.0],
        "role C":   [0.7, 0.7],
    })
    picks = schema_block.select_extra_tables(
        "question", available={"a_tbl", "b_tbl", "c_tbl"}, k=2
    )
    assert picks == ["b_tbl", "c_tbl"]


def test_select_extra_tables_stable_on_ties(schema_docs, fake_query_embed):
    schema_docs([
        {"table": "a_tbl", "role": "role A"},
        {"table": "b_tbl", "role": "role B"},
        {"table": "c_tbl", "role": "role C"},
    ])
    fake_query_embed({
        "question": [1.0, 0.0],
        "role A":   [1.0, 0.0],
        "role B":   [1.0, 0.0],
        "role C":   [1.0, 0.0],
    })
    picks = schema_block.select_extra_tables(
        "question", available={"a_tbl", "b_tbl", "c_tbl"}, k=2
    )
    assert picks == ["a_tbl", "b_tbl"]


def test_select_extra_tables_question_embed_none_falls_back_to_head(
    schema_docs, fake_query_embed
):
    schema_docs([
        {"table": "first",  "role": "role first"},
        {"table": "second", "role": "role second"},
        {"table": "third",  "role": "role third"},
    ])
    fake_query_embed({})  # 質問 embed = None
    picks = schema_block.select_extra_tables(
        "question", available={"first", "second", "third"}, k=2
    )
    assert picks == ["first", "second"]


def test_select_extra_tables_skips_items_without_role_embed(schema_docs, fake_query_embed):
    schema_docs([
        {"table": "a_tbl", "role": "role A"},
        {"table": "b_tbl", "role": "role B"},
    ])
    fake_query_embed({
        "question": [1.0, 0.0],
        # role A は embed できない → スキップ
        "role B":   [0.9, 0.1],
    })
    picks = schema_block.select_extra_tables(
        "question", available={"a_tbl", "b_tbl"}, k=3
    )
    assert picks == ["b_tbl"]


def test_select_extra_tables_filters_by_available(schema_docs, fake_query_embed):
    """available に含まれない表は YAML にあっても候補から外れる。"""
    schema_docs([
        {"table": "a_tbl", "role": "role A"},
        {"table": "b_tbl", "role": "role B"},
    ])
    fake_query_embed({
        "question": [1.0, 0.0],
        "role A": [1.0, 0.0],
        "role B": [1.0, 0.0],
    })
    picks = schema_block.select_extra_tables(
        "question", available={"b_tbl"}, k=2
    )
    assert picks == ["b_tbl"]


def test_select_extra_tables_empty_docs_returns_empty(schema_docs, fake_query_embed):
    schema_docs([])
    fake_query_embed({"question": [1.0, 0.0]})
    assert schema_block.select_extra_tables(
        "question", available={"any"}, k=3
    ) == []


# -----------------------------------------------------------------
# build_schema_block
# -----------------------------------------------------------------

def test_build_schema_block_includes_all_core_tables_in_order(
    tmp_db, schema_docs, fake_query_embed
):
    """Core 12 表が定義順 (documents → entity canonical → relation junction) で並ぶ。"""
    schema_docs([])  # extra 候補なし
    fake_query_embed({})
    block = schema_block.build_schema_block("dummy", db=tmp_db)

    positions: dict[str, int] = {}
    for name in schema_block.CORE_TABLES:
        idx = block.find(f"\n{name}(")
        assert idx != -1, f"core table {name} missing from block:\n{block}"
        positions[name] = idx
    # 順序確認
    ordered = sorted(positions, key=lambda n: positions[n])
    assert ordered == list(schema_block.CORE_TABLES)


def test_build_schema_block_section_headers_present(
    tmp_db, schema_docs, fake_query_embed
):
    schema_docs([
        {"table": "weak_relations", "role": "weak relations"},
    ])
    fake_query_embed({
        "dummy": [1.0, 0.0],
        "weak relations": [1.0, 0.0],
    })
    block = schema_block.build_schema_block("dummy", db=tmp_db)
    assert "-- 中核" in block
    assert "-- エンティティ canonical" in block
    assert "-- 関係 junction" in block
    assert "-- 関連 (質問に応じて自動選択" in block


def test_build_schema_block_excludes_internal_tables(
    tmp_db, schema_docs, fake_query_embed
):
    schema_docs([])
    fake_query_embed({})
    block = schema_block.build_schema_block("dummy", db=tmp_db)
    for name in schema_block.INTERNAL_TABLES:
        assert f"\n{name}(" not in block, f"internal table {name} leaked into block"
    # FTS5 shadow
    for suf in ("_data", "_idx", "_content", "_docsize", "_config"):
        assert f"\ndoc_fts{suf}(" not in block


def test_build_schema_block_product_has_typed_columns(
    tmp_db, schema_docs, fake_query_embed
):
    """元 doc の動機: product 行に trial_period_days 等の typed 列が並ぶこと。"""
    schema_docs([])
    fake_query_embed({})
    block = schema_block.build_schema_block("dummy", db=tmp_db)
    # product 行を抽出
    for line in block.splitlines():
        if line.startswith("product("):
            assert "trial_period_days INTEGER" in line
            assert "billing_period TEXT" in line
            return
    pytest.fail("product canonical row not found in schema block")


def test_build_schema_block_extra_top_k_respects_config(
    tmp_db, schema_docs, fake_query_embed, monkeypatch
):
    """質問に高 cos-sim な extras が prompt に注入される (top-K = config.SCHEMA_EXTRA_TOPK)。"""
    monkeypatch.setattr(config, "SCHEMA_EXTRA_TOPK", 2)
    schema_docs([
        {"table": "product_claims", "role": "product 属性履歴"},
        {"table": "product_aliases", "role": "product 別名"},
        {"table": "weak_relations", "role": "型不明な弱関係"},
    ])
    fake_query_embed({
        "Bob の無料期間は？": [1.0, 0.0],
        "product 属性履歴":     [0.9, 0.1],
        "product 別名":         [0.8, 0.2],
        "型不明な弱関係":       [0.0, 1.0],
    })
    block = schema_block.build_schema_block("Bob の無料期間は？", db=tmp_db)
    assert "\nproduct_claims(" in block
    assert "\nproduct_aliases(" in block
    # top-2 なので weak_relations は extras に出ない
    assert "\nweak_relations(" not in block
