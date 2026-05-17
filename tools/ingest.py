"""取り込みパイプライン（グラフ版）。スクリプトが制御し、SLM は狭いタスクのみ。

  python tools/ingest.py raw/extracted/議事録.md

手順:
  1. 分割要約（map-reduce, SLM）
  2. グラフ抽出: entities/relations/facts を JSON で（SLM）
  3. 突合: ルール正規化 → 曖昧時のみ SLM 判定 → DB へ（決定的中心）
  4. 関数的述語・属性の矛盾をルール検出（非破壊で conflict_group 化）
  5. log 追記 → 人間用レビュー要約（矛盾・新規実体を提示）
"""
import sys
import textwrap

import config
import llm
import db as kg
import logadd


def load_prompt(name):
    return (config.PROMPTS / name).read_text(encoding="utf-8")


def chunk(text, size):
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def summarize(text):
    tmpl = load_prompt("summarize.txt")
    parts = [llm.ask(tmpl.replace("{CONTENT}", c)) for c in chunk(text, config.CHUNK_CHARS)]
    return parts[0] if len(parts) == 1 else llm.ask(tmpl.replace("{CONTENT}", "\n\n".join(parts)))


def extract_graph(summary):
    g = llm.ask_json(load_prompt("extract_graph.txt").replace("{CONTENT}", summary))
    if isinstance(g, list):  # 弱いモデルが配列を返す保険
        g = {"entities": g, "relations": [], "facts": []}
    return (g.get("entities") or [], g.get("relations") or [], g.get("facts") or [])


def make_adjudicator():
    tmpl = load_prompt("dedup_adjudicate.txt")

    def adjudicate(a, b, etype):
        out = (
            llm.ask(tmpl.replace("{TYPE}", etype).replace("{NAME_A}", a).replace("{NAME_B}", b))
            .strip()
            .lower()
        )
        return "same" if out.startswith("same") else "different"

    return adjudicate


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/ingest.py raw/extracted/<file>.md")
        return
    src = config.ROOT / sys.argv[1]
    body = src.read_text(encoding="utf-8", errors="replace")
    slug = src.stem
    title = slug.replace("-", " ")

    print(f"[1] 要約: {src.name}")
    summary = summarize(body)

    print("[2] グラフ抽出")
    ents, rels, facts = extract_graph(summary)
    print(f"  entities={len(ents)} relations={len(rels)} facts={len(facts)}")

    db = kg.connect()
    doc_id = kg.upsert_document(db, slug, title, str(src), summary, body)
    adjud = make_adjudicator()

    print("[3] 突合（ルール → 曖昧時のみ SLM）")
    name_to_id, how_count = {}, {}
    for e in ents:
        if not e.get("name"):
            continue
        eid, how = kg.find_or_stage_entity(
            db, e["name"], e.get("type", "concept"), adjudicate=adjud
        )
        name_to_id[e["name"]] = eid
        kg.add_mention(db, eid, doc_id, e["name"], summary[:200])
        how_count[how] = how_count.get(how, 0) + 1

    def resolve(nm):
        if nm not in name_to_id:
            eid, _ = kg.find_or_stage_entity(db, nm, "concept", adjudicate=adjud)
            name_to_id[nm] = eid
        return name_to_id[nm]

    print("[4] 関係・属性の登録と矛盾検出")
    conflicts = []
    for r in rels:
        if not (r.get("subject") and r.get("object") and r.get("predicate")):
            continue
        _, st = kg.add_relation(
            db, resolve(r["subject"]), r["predicate"], resolve(r["object"]),
            doc_id, r.get("evidence", ""),
        )
        if st == "conflict":
            conflicts.append(f"関係矛盾: {r['subject']} -{r['predicate']}-> {r['object']}")
    for f in facts:
        if not (f.get("entity") and f.get("attribute")):
            continue
        _, st = kg.add_fact(db, resolve(f["entity"]), f["attribute"], f.get("value", ""), doc_id)
        if st == "conflict":
            conflicts.append(f"属性矛盾: {f['entity']}.{f['attribute']} = {f.get('value')}")

    db.commit()
    logadd.add(
        "ingest", title,
        f"entities={len(ents)} relations={len(rels)} facts={len(facts)} conflicts={len(conflicts)}",
    )

    print("\n===== レビュー要約（人間が確認 → PR レビュー）=====")
    print(f"文書: {title} (slug={slug})")
    print(f"突合内訳: {how_count}  ※ new=新規 / llm-merge=SLM が同一判定 / exact,alias=規則一致")
    if conflicts:
        print("検出された矛盾（DB は非破壊保存・status=conflicted）:")
        for c in conflicts:
            print("  -", c)
    else:
        print("矛盾なし")
    print("要約冒頭:\n" + textwrap.indent(summary[:600], "  "))
    print("==================================================")


if __name__ == "__main__":
    main()
