"""健康診断。矛盾・孤立実体・統計を提示するだけ（自動修正しない）。

  python tools/lint.py
"""
import db as kg


def main():
    db = kg.connect(readonly=True)

    n_doc = db.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
    n_ent = db.execute(
        "SELECT COUNT(*) c FROM entities WHERE status!='merged'"
    ).fetchone()["c"]
    n_rel = db.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
    print(f"文書={n_doc} 実体={n_ent} 関係={n_rel}")

    rel_c, fact_c = kg.open_conflicts(db)
    print(f"\n[矛盾] 関係グループ={len(rel_c)} 属性グループ={len(fact_c)}")
    for c in rel_c:
        members = db.execute(
            "SELECT e1.canonical_name s, r.predicate p, e2.canonical_name o, d.slug src "
            "FROM relations r JOIN entities e1 ON r.subject_id=e1.id "
            "JOIN entities e2 ON r.object_id=e2.id JOIN documents d ON r.document_id=d.id "
            "WHERE r.conflict_group=?",
            (c["conflict_group"],),
        ).fetchall()
        print(f"  - グループ {c['conflict_group']}:")
        for m in members:
            print(f"      {m['s']} -{m['p']}-> {m['o']}  ({m['src']})")
    for c in fact_c:
        members = db.execute(
            "SELECT e.canonical_name n, f.attribute a, f.value v, d.slug src "
            "FROM facts f JOIN entities e ON f.entity_id=e.id "
            "JOIN documents d ON f.document_id=d.id WHERE f.conflict_group=?",
            (c["conflict_group"],),
        ).fetchall()
        print(f"  - グループ {c['conflict_group']}:")
        for m in members:
            print(f"      {m['n']}.{m['a']} = {m['v']}  ({m['src']})")

    orphans = db.execute(
        "SELECT canonical_name FROM entities e WHERE status!='merged' "
        "AND NOT EXISTS (SELECT 1 FROM relations r "
        "WHERE r.subject_id=e.id OR r.object_id=e.id) "
        "AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.entity_id=e.id)"
    ).fetchall()
    print(f"\n[孤立実体] {len(orphans)} 件")
    for o in orphans[:20]:
        print(f"  - {o['canonical_name']}")


if __name__ == "__main__":
    main()
