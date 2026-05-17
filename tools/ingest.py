"""取り込みパイプライン（グラフ版）。スクリプトが制御し、SLM は狭いタスクのみ。

  python tools/ingest.py raw/extracted/議事録.md

手順:
  1. semantic chunking（markdown header / 段落境界を尊重）
  2. グラフ抽出: entities/relations/facts を JSON で（SLM, per-chunk + running entity hint）
  3. 突合: ルール正規化 → 曖昧時のみ SLM 判定 → DB へ（決定的中心）
  4. 関数的述語・属性の矛盾をルール検出（非破壊で conflict_group 化）
  5. log 追記 → 人間用レビュー要約（矛盾・新規実体を提示）
"""
import re
import sys
import textwrap

import config
import llm
import db as kg
import logadd


_SCALAR_RE = re.compile(r"^(約|およそ)?[\d０-９]")


def _looks_scalar_or_long(s: str) -> bool:
    s = (s or "").strip()
    return bool(s) and (bool(_SCALAR_RE.match(s)) or len(s) > 30)


def load_prompt(name):
    return (config.PROMPTS / name).read_text(encoding="utf-8")


def _char_split(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _split_by_header_level(text: str, level: int) -> list[str]:
    """Markdown を指定レベル（例 ## なら 2）のヘッダ位置で切る。該当ヘッダが無ければ単一要素を返す。"""
    pat = re.compile(rf"(?m)^#{{{level}}}\s")
    starts = [m.start() for m in pat.finditer(text)]
    if not starts:
        return [text]
    sections = []
    if starts[0] != 0:
        sections.append(text[: starts[0]])
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        sections.append(text[s:e])
    return [s for s in sections if s.strip()]


def _split_recursive(text: str, max_chars: int, levels: list[int]) -> list[str]:
    """levels の順にヘッダで切り、max_chars 超のセクションだけさらに深いレベルへ再帰。"""
    if len(text) <= max_chars or not levels:
        return [text]
    level = levels[0]
    parts = _split_by_header_level(text, level)
    if len(parts) == 1:
        return _split_recursive(text, max_chars, levels[1:])
    out = []
    for p in parts:
        if len(p) <= max_chars:
            out.append(p)
        else:
            out.extend(_split_recursive(p, max_chars, levels[1:]))
    return out


def _greedy_pack(units: list[str], max_chars: int, sep: str = "\n\n") -> list[str]:
    """隣接 unit を max_chars を超えない範囲で結合。"""
    out, cur, cur_len = [], [], 0
    for u in units:
        ul = len(u)
        if not cur:
            cur, cur_len = [u], ul
            continue
        if cur_len + len(sep) + ul > max_chars:
            out.append(sep.join(cur))
            cur, cur_len = [u], ul
        else:
            cur.append(u)
            cur_len += len(sep) + ul
    if cur:
        out.append(sep.join(cur))
    return out


def semantic_chunk(text: str, max_chars: int) -> list[str]:
    """markdown header → 段落 → 文字数 の優先順で意味的に分割。"""
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    if re.search(r"(?m)^#{1,6}\s", text):
        units = _split_recursive(text, max_chars, [2, 3, 4, 5, 6])
    else:
        units = [u for u in re.split(r"\n\s*\n", text) if u.strip()] or [text]

    expanded = []
    for u in units:
        if len(u) <= max_chars:
            expanded.append(u)
            continue
        paras = [p for p in re.split(r"\n\s*\n", u) if p.strip()]
        for p in paras:
            if len(p) <= max_chars:
                expanded.append(p)
            else:
                expanded.extend(_char_split(p, max_chars))

    return _greedy_pack(expanded, max_chars)


def _format_known_entities(known: list[dict]) -> str:
    if not known:
        return ""
    block = "\n".join(f"- {e['name']} ({e.get('type', 'concept')})" for e in known)
    return (
        "このドキュメント内で既に登場したエンティティ"
        "（同一対象を指す新しい言及があれば、これらの表記を再利用してください）:\n"
        f"{block}\n\n"
    )


def extract_graph(text: str, known_entities: list[dict] | None = None):
    tmpl = load_prompt("extract_graph.txt")
    prompt = tmpl.replace("{KNOWN_ENTITIES}", _format_known_entities(known_entities or [])).replace(
        "{CONTENT}", text
    )
    g = llm.ask_json(prompt)
    if isinstance(g, list):  # 弱いモデルが配列を返す保険
        g = {"entities": g, "relations": [], "facts": []}
    return (g.get("entities") or [], g.get("relations") or [], g.get("facts") or [])


def _dedup_entities_by_name(ents: list[dict]) -> list[dict]:
    """同一 name は最初に出たものを採用（type も最初のものを保持）。"""
    seen, out = set(), []
    for e in ents:
        nm = e.get("name")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        out.append(e)
    return out


def extract_graph_from_chunks(chunks: list[str]):
    """単一 chunk なら full-text 1パス、複数なら per-chunk + running entity hint。"""
    if len(chunks) == 1:
        return extract_graph(chunks[0])

    ents, rels, facts = [], [], []
    seen, seen_names = [], set()
    for c in chunks:
        e, r, f = extract_graph(c, known_entities=seen)
        for ent in e:
            nm = ent.get("name")
            if nm and nm not in seen_names:
                seen.append(ent)
                seen_names.add(nm)
        ents.extend(e)
        rels.extend(r)
        facts.extend(f)
    return _dedup_entities_by_name(ents), rels, facts


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

    chunks = semantic_chunk(body, config.CHUNK_CHARS)
    print(f"[1] semantic chunking: {src.name} → {len(chunks)} chunk(s)")

    print("[2] グラフ抽出（per-chunk + running entity hint）" if len(chunks) > 1 else "[2] グラフ抽出（full-text 1パス）")
    ents, rels, facts = extract_graph_from_chunks(chunks)
    print(f"  entities={len(ents)} relations={len(rels)} facts={len(facts)}")

    db = kg.connect()
    doc_id = kg.upsert_document(db, slug, title, str(src), body)
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
        kg.add_mention(db, eid, doc_id, e["name"])
        how_count[how] = how_count.get(how, 0) + 1

    def resolve(nm):
        if nm not in name_to_id:
            eid, _ = kg.find_or_stage_entity(db, nm, "concept", adjudicate=adjud)
            name_to_id[nm] = eid
        return name_to_id[nm]

    print("[4] 関係・属性の登録と矛盾検出")
    conflicts = []
    redirected = 0
    for r in rels:
        if not (r.get("subject") and r.get("object") and r.get("predicate")):
            continue
        if _looks_scalar_or_long(r["object"]):
            _, st = kg.add_fact(
                db, resolve(r["subject"]), r["predicate"], r["object"], doc_id
            )
            redirected += 1
            if st == "conflict":
                conflicts.append(f"属性矛盾: {r['subject']}.{r['predicate']} = {r['object']}")
            continue
        _, st = kg.add_relation(
            db, resolve(r["subject"]), r["predicate"], resolve(r["object"]),
            doc_id, r.get("evidence", ""),
        )
        if st == "conflict":
            conflicts.append(f"関係矛盾: {r['subject']} -{r['predicate']}-> {r['object']}")
    if redirected:
        print(f"  関係 → 属性へ振替: {redirected} 件（数値・日付・長文 object）")
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
    print("本文冒頭:\n" + textwrap.indent(body[:600], "  "))
    print("==================================================")


if __name__ == "__main__":
    main()
