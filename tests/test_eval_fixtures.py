"""tools/eval/fixtures.py build_fixture が typed gold から正しい snapshot を作る."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_build_fixture_from_typed_gold(tmp_path, monkeypatch):
    import config
    # config.KG_DB は build_fixture が redirect_kg_db で書き換える
    out = tmp_path / "acme.sqlite"

    from tools.eval import fixtures
    monkeypatch.setattr(fixtures, "ALLOWED_DB_ROOTS", (tmp_path.resolve(),))
    fixtures.build_fixture("data/eval/gold/extract/acme-overview.yml", out)
    assert out.exists()

    c = sqlite3.connect(out)
    c.row_factory = sqlite3.Row
    assert c.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM organization").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM product").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM employment").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM manufacturing").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM org_hierarchy").fetchone()[0] == 1
    # weak_relations: 業務提携 / 共同推進
    n_weak = c.execute("SELECT COUNT(DISTINCT predicate) FROM weak_relations").fetchone()[0]
    assert n_weak >= 1
    # employment.role が claim 経由で canonical に反映されていること
    roles = {r["role"] for r in c.execute("SELECT role FROM employment")}
    assert roles == {"CEO", "CTO"}
    c.close()


def test_dedup_eval_runs(tmp_path, monkeypatch):
    import config
    from tools.eval import fixtures
    monkeypatch.setattr(fixtures, "ALLOWED_DB_ROOTS", (tmp_path.resolve(),))
    monkeypatch.setattr(fixtures, "RUNTIME_DIR", tmp_path)
    from tools.eval import dedup_eval
    res = dedup_eval.evaluate("data/eval/gold/dedup/pairs.yml", runs=1)
    assert res["per_run"][0]["n_pairs"] == 13
    # typed pipeline は exact のみ実現可能。alias-or-llm は new で fall-through する仕様
    assert res["per_run"][0]["accuracy"] > 0.7
