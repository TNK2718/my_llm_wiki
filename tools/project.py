"""DB（正本）から read-only の markdown ページを射影する。Obsidian 閲覧用。

  python tools/project.py

wiki/ 配下は毎回再生成される派生物。手で編集しない（DB を直すか再取り込み）。
"""
import shutil
from datetime import date

import config
import db as kg


def main():
    for sub in ("entities", "concepts"):
        d = config.WIKI / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    db = kg.connect(readonly=True)
    ents = db.execute(
        "SELECT id, canonical_name, type, status FROM entities WHERE status!='merged'"
    ).fetchall()

    index = ["# Index", "", f"_DB から自動生成 {date.today()}。手で編集しない。_", ""]
    for e in sorted(ents, key=lambda r: r["type"]):
        sub = "concepts" if e["type"] == "concept" else "entities"
        rels = db.execute(
            "SELECT r.predicate p, e2.canonical_name o, d.slug src, r.status st "
            "FROM relations r JOIN entities e2 ON r.object_id=e2.id "
            "JOIN documents d ON r.document_id=d.id WHERE r.subject_id=?",
            (e["id"],),
        ).fetchall()
        facts = db.execute(
            "SELECT f.attribute a, f.value v, d.slug src, f.status st "
            "FROM facts f JOIN documents d ON f.document_id=d.id WHERE f.entity_id=?",
            (e["id"],),
        ).fetchall()
        srcs = sorted({r["src"] for r in rels} | {f["src"] for f in facts})

        body = [
            "---",
            f"title: {e['canonical_name']}",
            f"type: {e['type']}",
            f"sources: [{', '.join(srcs)}]",
            f"updated: {date.today()}",
            "---",
            "",
            f"# {e['canonical_name']}",
            "",
            "## 属性",
        ]
        for f in facts:
            flag = " ⚠️[矛盾]" if f["st"] == "conflicted" else ""
            body.append(f"- **{f['a']}**: {f['v']} (出典: [[sources/{f['src']}]]){flag}")
        body += ["", "## 関係"]
        for r in rels:
            flag = " ⚠️[矛盾]" if r["st"] == "conflicted" else ""
            body.append(
                f"- {r['p']} → {r['o']} (出典: [[sources/{r['src']}]]){flag}"
            )
        slug = f"{e['id']}-{kg.normalize(e['canonical_name'])[:40] or 'x'}"
        (config.WIKI / sub / f"{slug}.md").write_text("\n".join(body), encoding="utf-8")
        index.append(f"- [[{sub}/{slug}]] — {e['canonical_name']} ({e['type']})")

    (config.WIKI / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"射影完了: {len(ents)} ページ → wiki/")


if __name__ == "__main__":
    main()
