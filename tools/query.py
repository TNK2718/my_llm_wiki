"""質問応答。テンプレート優先 → 検証付き text2sql → 文書全文 → 回答生成。

CLI:
  python tools/query.py "Acme の CEO は誰？"
  python tools/query.py --sql "解約率の推移"

サーバからは answer_question() を呼ぶ。
"""
import hashlib
import re
import sqlite3
import struct
import sys

import config
import llm
import db as kg

TEMPLATES = {
    "relations_of": (
        "SELECT e1.canonical_name s, r.predicate p, e2.canonical_name o, "
        "d.slug src, r.status FROM relations r "
        "JOIN entities e1 ON r.subject_id=e1.id JOIN entities e2 ON r.object_id=e2.id "
        "JOIN documents d ON r.document_id=d.id "
        "WHERE (e1.canonical_name LIKE ? OR e2.canonical_name LIKE ?) LIMIT 30"
    ),
    "facts_of": (
        "SELECT e.canonical_name n, f.attribute a, f.value v, d.slug src, f.status "
        "FROM facts f JOIN entities e ON f.entity_id=e.id "
        "JOIN documents d ON f.document_id=d.id "
        "WHERE e.canonical_name LIKE ? LIMIT 30"
    ),
}

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b", re.I)


def validate_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if ";" in sql:
        raise ValueError("複文は不可")
    if not re.match(r"(?is)^\s*select\b", sql):
        raise ValueError("SELECT のみ許可")
    if FORBIDDEN.search(sql):
        raise ValueError("書き込み/DDL 系キーワードは不可")
    if not re.search(r"(?i)\blimit\b", sql):
        sql += " LIMIT 50"
    return sql


def run_ro(sql: str):
    db = kg.connect(readonly=True)
    db.execute(f"EXPLAIN {sql}")
    return [dict(r) for r in db.execute(sql).fetchall()]


def _embed_cache_conn() -> sqlite3.Connection:
    config.EMBED_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.EMBED_CACHE_DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS embed_cache("
        " key TEXT PRIMARY KEY, vec BLOB NOT NULL)"
    )
    return c


def _embed_cached(text: str) -> list[float] | None:
    if not text:
        return None
    key = hashlib.sha1(f"{config.EMBED_MODEL}|{text}".encode("utf-8")).hexdigest()
    c = _embed_cache_conn()
    row = c.execute("SELECT vec FROM embed_cache WHERE key=?", (key,)).fetchone()
    if row:
        blob = row[0]
        n = len(blob) // 4
        return list(struct.unpack(f"{n}f", blob))
    v = llm.embed(text)
    if v is None:
        return None
    blob = struct.pack(f"{len(v)}f", *v)
    c.execute("INSERT OR REPLACE INTO embed_cache(key,vec) VALUES(?,?)", (key, blob))
    c.commit()
    return v


def _cosine(a: list[float], b: list[float]) -> float:
    s = sa = sb = 0.0
    for x, y in zip(a, b):
        s += x * y
        sa += x * x
        sb += y * y
    if sa == 0 or sb == 0:
        return 0.0
    return s / ((sa ** 0.5) * (sb ** 0.5))


def column_hints(question: str) -> str:
    """text2sql 修復用のヒント文字列。失敗時のみ呼ぶ前提（DB 全値 scan を含むため）。

    - 自由値列（canonical_name, alias, facts.value）は Trigram と埋め込みの和集合で top-K。
    - 小規模カテゴリ列（type/predicate/attribute）は distinct 値の語彙を列挙。
    - Ollama embed が落ちていれば埋め込み信号は無効化し、Trigram のみで縮退する。
    """
    qn = kg.normalize(question)
    if not qn:
        return ""
    q_vec = _embed_cached(question)

    db = kg.connect(readonly=True)
    lines: list[str] = []

    free_cols = [
        ("entities.canonical_name",
         "SELECT DISTINCT canonical_name AS v, norm_key AS nk "
         "FROM entities WHERE status!='merged'"),
        ("entity_aliases.alias",
         "SELECT DISTINCT alias AS v, norm_key AS nk FROM entity_aliases"),
        ("facts.value",
         "SELECT DISTINCT value AS v FROM facts "
         "WHERE status='active' AND value IS NOT NULL"),
    ]
    for col, sql in free_cols:
        scored = []
        for r in db.execute(sql):
            v = r["v"]
            if not v:
                continue
            keys = r.keys()
            nk = r["nk"] if "nk" in keys and r["nk"] else kg.normalize(v)
            sim_tri = kg.similarity(qn, nk)
            contained = 1.0 if (nk and nk in qn) else 0.0
            sim_emb = 0.0
            if q_vec is not None:
                v_vec = _embed_cached(v)
                if v_vec is not None and len(v_vec) == len(q_vec):
                    sim_emb = _cosine(q_vec, v_vec)
            score = max(contained, sim_tri, sim_emb)
            keep = (
                contained == 1.0
                or sim_tri >= config.HINT_TRIGRAM_THRESHOLD
                or sim_emb >= config.HINT_EMBED_THRESHOLD
            )
            if keep:
                scored.append((score, v))
        scored.sort(key=lambda t: -t[0])
        if scored:
            picks = [f"'{v}'" for _, v in scored[: config.HINT_TOPK_PER_COLUMN]]
            lines.append(f"- {col} に近い候補: " + ", ".join(picks))

    vocab_cols = [
        ("entities.type",
         "SELECT DISTINCT type AS v FROM entities WHERE status!='merged'"),
        ("relations.predicate",
         "SELECT DISTINCT predicate AS v FROM relations WHERE status='active'"),
        ("facts.attribute",
         "SELECT DISTINCT attribute AS v FROM facts WHERE status='active'"),
    ]
    for col, sql in vocab_cols:
        vals = [r["v"] for r in db.execute(sql) if r["v"]]
        if vals:
            shown = vals[: config.HINT_VOCAB_CAP]
            tail = ", ..." if len(vals) > config.HINT_VOCAB_CAP else ""
            lines.append(f"- {col} の語彙: " + ", ".join(shown) + tail)

    if not lines:
        return ""
    block = "ヒント（DB 内の近い値 / 列の語彙）:\n" + "\n".join(lines)
    return block[: config.HINT_MAX_CHARS]


def text2sql(question: str):
    tmpl = (config.PROMPTS / "text2sql.txt").read_text(encoding="utf-8")
    sql = re.sub(r"```sql|```", "", llm.ask(tmpl.replace("{QUESTION}", question))).strip()
    try:
        sql = validate_sql(sql)
        return sql, run_ro(sql)
    except (ValueError, sqlite3.Error) as e1:
        hints = column_hints(question)
        hint_block = f"\n\n{hints}" if hints else ""
        fix = llm.ask(
            tmpl.replace("{QUESTION}", question)
            + f"\n\n直前の SQL はエラー: {e1}"
            + hint_block
            + "\n修正後の SQL のみ:"
        )
        sql = validate_sql(re.sub(r"```sql|```", "", fix).strip())
        return sql, run_ro(sql)


def find_entity_term(question: str):
    db = kg.connect(readonly=True)
    for r in db.execute("SELECT canonical_name FROM entities WHERE status!='merged'"):
        if r["canonical_name"] and r["canonical_name"] in question:
            return r["canonical_name"]
    return None


def template_rows(question: str):
    term = find_entity_term(question)
    if not term:
        return None
    like = f"%{term}%"
    db = kg.connect(readonly=True)
    out = [dict(r) for r in db.execute(TEMPLATES["relations_of"], (like, like))]
    out += [dict(r) for r in db.execute(TEMPLATES["facts_of"], (like,))]
    return out or None


def fts_docs(question: str, k: int = 4):
    db = kg.connect(readonly=True)
    q = " OR ".join(re.findall(r"\w{2,}", question))[:200] or question
    try:
        rows = db.execute(
            "SELECT slug, snippet(doc_fts,2,'>>','<<','…',15) s "
            "FROM doc_fts WHERE doc_fts MATCH ? LIMIT ?",
            (q, k),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    return [{"slug": r["slug"], "snippet": r["s"]} for r in rows]


def answer_question(question: str, force_sql: bool = False) -> dict:
    """構造化結果・SQL・文書・回答をまとめて返す（サーバ/CLI 共用）。"""
    sql_used, route, error = None, "template", None
    rows = None if force_sql else template_rows(question)
    if rows is None:
        try:
            sql_used, rows = text2sql(question)
            route = "text2sql"
        except (ValueError, sqlite3.Error) as e:
            rows, route, error = [], "fallback", f"text2sql 失敗: {e}"
    docs = fts_docs(question)

    answer = ""
    try:
        prompt = (
            (config.PROMPTS / "answer.txt")
            .read_text(encoding="utf-8")
            .replace("{QUESTION}", question)
            .replace("{ROWS}", str(rows)[:4000] or "(なし)")
            .replace("{DOCS}", "\n".join(f"({d['slug']}) {d['snippet']}" for d in docs) or "(なし)")
        )
        answer = llm.ask(prompt)
    except Exception as e:  # noqa: BLE001  Ollama 未起動など
        error = (error + " / " if error else "") + f"回答生成失敗: {e}"

    return {
        "question": question,
        "route": route,
        "sql": sql_used,
        "rows": rows,
        "docs": docs,
        "answer": answer,
        "error": error,
    }


def main():
    args = sys.argv[1:]
    force = bool(args) and args[0] == "--sql"
    if force:
        args = args[1:]
    if not args:
        print(__doc__)
        return
    res = answer_question(" ".join(args), force_sql=force)
    if res["sql"]:
        print(f"[SQL/{res['route']}] {res['sql']}\n")
    if res["error"]:
        print(f"[警告] {res['error']}\n")
    print(res["answer"] or "(回答生成不可)")


if __name__ == "__main__":
    main()
