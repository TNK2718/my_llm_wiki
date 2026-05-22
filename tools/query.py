"""質問応答。typed schema 用 text2sql + R1/R2 linter + FTS fallback.

CLI:
  python tools/query.py "Acme の CEO は誰？"

サーバからは answer_question() を呼ぶ。
"""
import functools
import hashlib
import re
import sqlite3
import struct
import sys

import yaml

import config
import db as kg
import llm
import sql_linter


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
    # §8 linter R1 / autofix R2 (typed-schema-design)
    sql = sql_linter.autofix_or_raise(sql)
    return sql


def run_ro(sql: str):
    db = kg.connect(readonly=True)
    db.execute(f"EXPLAIN {sql}")
    return [dict(r) for r in db.execute(sql).fetchall()]


# ---------- embed cache (旧実装を流用) ----------
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


# ---------- typed schema 用 column_hints ----------
_ENTITY_TABLES = ("person", "organization", "product", "project")


def column_hints(question: str) -> str:
    """text2sql 修復用のヒント。typed schema では canonical_name と alias を中心に拾う。"""
    qn = kg.normalize(question)
    if not qn:
        return ""
    q_vec = _embed_cached(question)

    db = kg.connect(readonly=True)
    lines: list[str] = []

    for tbl in _ENTITY_TABLES:
        free_cols = [
            (f"{tbl}.canonical_name",
             f"SELECT DISTINCT canonical_name AS v, norm_key AS nk FROM {tbl}"),
            (f"{tbl}_aliases.alias",
             f"SELECT DISTINCT alias AS v, norm_key AS nk FROM {tbl}_aliases"),
        ]
        for col, sql in free_cols:
            scored = []
            for r in db.execute(sql):
                v = r["v"]
                if not v:
                    continue
                nk = r["nk"] if r["nk"] else kg.normalize(v)
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

    # *_claims.column_name と enum 列の語彙 (schema CHECK 由来の固定値)
    vocab = [
        ("organization.org_type",
         ("company", "lab", "team", "university", "government", "nonprofit", "other")),
        ("person_claims.column_name", ("canonical_name", "birth_date", "nationality")),
        ("organization_claims.column_name",
         ("canonical_name", "org_type", "founded_year", "headquarters")),
        ("product_claims.column_name",
         ("canonical_name", "release_date", "category",
          "billing_period", "included_quota_units", "quota_unit_name", "trial_period_days")),
        ("project_claims.column_name", ("canonical_name", "started_at", "ended_at")),
        ("contract_claims.column_name",
         ("canonical_name", "contract_type", "effective_date", "valid_until",
          "jurisdiction", "url", "version",
          "sla_uptime_percent", "sla_response_time_minutes",
          "rto_hours", "rpo_hours")),
        ("employment_claims.column_name", ("role", "end_date")),
    ]
    for col, vals in vocab:
        if vals:
            lines.append(f"- {col} の語彙: " + ", ".join(vals))

    if not lines:
        return ""
    block = "ヒント（DB 内の近い値 / 列の語彙）:\n" + "\n".join(lines)
    return block[: config.HINT_MAX_CHARS]


# ---------- Fewshot 動的選択 ----------
@functools.cache
def _load_fewshot_pool() -> tuple[dict, ...]:
    """data/fewshot/text2sql.yml を読んで items を返す。
    ファイル不在 / 空 / version 不一致 → 空タプル。
    tuple を返すのは @cache 用 (mutable list を返すと呼び出し側で破壊できてしまう)。"""
    path = config.FEWSHOT_POOL
    if not path.exists():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ()
    if data.get("version") != 1:
        return ()
    items = data.get("items") or []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        q, sql = it.get("q"), it.get("sql")
        if not q or not sql:
            continue
        out.append({
            "id": it.get("id") or f"_anon_{len(out)}",
            "q": q,
            "sql": sql,
            "note": it.get("note") or "",
        })
    return tuple(out)


def select_fewshots(question: str, k: int) -> list[dict]:
    """質問に近い Fewshot を embedding cos-sim で top-k 取る。

    - プールが空 → []
    - 質問 embed が None → プール先頭 k 件 (CI で Ollama 不在でも動かす)
    - item 側 embed が None → cos-sim = -inf 扱いで末尾送り (= 取得しない)
    """
    pool = _load_fewshot_pool()
    if not pool or k <= 0:
        return []
    q_vec = _embed_cached(question)
    if q_vec is None:
        return [dict(it) for it in pool[:k]]
    scored: list[tuple[float, int, dict]] = []
    for i, it in enumerate(pool):
        v = _embed_cached(it["q"])
        if v is None or len(v) != len(q_vec):
            continue
        scored.append((_cosine(q_vec, v), i, it))
    # 降順、同点は YAML 出現順 (i 昇順) で安定化
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [dict(it) for _, _, it in scored[:k]]


def _format_fewshots(items: list[dict]) -> str:
    """text2sql.txt の旧 fixed 3 例と同形式で 1-indexed に整形。空なら ""。"""
    if not items:
        return ""
    lines: list[str] = []
    for i, it in enumerate(items, start=1):
        lines.append(f'例 {i}) 質問: 「{it["q"]}」')
        lines.append(f'SQL: {it["sql"]}')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------- text2sql + linter retry ----------
def text2sql(question: str):
    """1) hint 付きプロンプト → validate (R1/R2 含む) → run。
    2) エラー or 0 行なら lint 違反内容/エラー文を追記して 1 回 retry (hints は再利用)。
    3) それでも失敗なら呼び出し元で FTS fallback。
    """
    import schema_block  # 遅延 import: schema_block が query._embed_cached を参照するため循環回避

    tmpl = (config.PROMPTS / "text2sql.txt").read_text(encoding="utf-8")
    hints = column_hints(question)
    llm.note("text2sql.hints", text=hints, has_hints=bool(hints))
    hint_block = f"{hints}\n\n" if hints else ""
    fewshots = select_fewshots(question, k=config.FEWSHOT_TOPK)
    llm.note("text2sql.fewshots", ids=[f["id"] for f in fewshots])
    fewshot_block = _format_fewshots(fewshots)
    schema = schema_block.build_schema_block(question)
    llm.note("text2sql.schema", n_bytes=len(schema))
    base_prompt = (
        tmpl.replace("{SCHEMA}", schema)
        .replace("{HINTS}", hint_block)
        .replace("{FEWSHOTS}", fewshot_block)
        .replace("{QUESTION}", question)
    )

    with llm.ask_as("text2sql/attempt1"):
        sql_raw = re.sub(r"```sql|```", "", llm.ask(base_prompt)).strip()
    llm.note("text2sql.sql_raw", sql=sql_raw)

    sql, rows, err = None, None, None
    try:
        sql = validate_sql(sql_raw)
        llm.note("text2sql.validate", attempt=1, sql=sql, ok=True)
        rows = run_ro(sql)
        llm.note("text2sql.run", attempt=1, n_rows=len(rows))
        if rows:
            return sql, rows
    except (ValueError, sqlite3.Error) as e:
        err = e
        llm.note("text2sql.validate" if isinstance(e, ValueError) else "text2sql.run",
                 attempt=1, ok=False, error=f"{type(e).__name__}: {e}")

    llm.note("text2sql.retry_decision", reason="error" if err is not None else "zero_rows")

    err_block = ""
    if err is not None:
        err_block = f"\n\n直前の SQL はエラー: {err}\n"
        # linter エラーは内容を表示してプロンプトに反映
        if "linter" in str(err).lower():
            err_block += "上記 linter ルール R1/R2 を遵守して書き直すこと。\n"
    elif sql is not None:
        err_block = (
            f"\n\n直前の SQL は構文OKだが 0 行だった:\n{sql}\n"
            "LIKE パターンが質問語の英訳/言い換えで DB の実値と一致していない可能性が高い。"
            "上のヒント候補語をそのまま LIKE のパターンに採用すること。\n"
        )

    if not err_block:
        if err is not None:
            raise err
        return sql, rows  # rows == [] 確定

    with llm.ask_as("text2sql/attempt2"):
        fix = llm.ask(base_prompt + err_block + "\n修正後の SQL のみ:")
    llm.note("text2sql.sql_raw", attempt=2, sql=fix)
    try:
        sql2 = validate_sql(re.sub(r"```sql|```", "", fix).strip())
        llm.note("text2sql.validate", attempt=2, sql=sql2, ok=True)
        rows2 = run_ro(sql2)
        llm.note("text2sql.run", attempt=2, n_rows=len(rows2))
    except (ValueError, sqlite3.Error) as e2:
        llm.note("text2sql.validate" if isinstance(e2, ValueError) else "text2sql.run",
                 attempt=2, ok=False, error=f"{type(e2).__name__}: {e2}")
        if err is None:
            return sql, rows
        raise err
    if rows2 or err is not None:
        return sql2, rows2
    return sql, rows


def fts_docs(question: str, k: int = 4):
    db = kg.connect(readonly=True)
    q = " OR ".join(re.findall(r"\w{2,}", question))[:200] or question
    try:
        rows = db.execute(
            "SELECT d.slug, snippet(doc_fts,1,'>>','<<','…',15) s "
            "FROM doc_fts JOIN documents d ON d.id=doc_fts.rowid "
            "WHERE doc_fts MATCH ? LIMIT ?",
            (q, k),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    return [{"slug": r["slug"], "snippet": r["s"]} for r in rows]


_ENTITY_TABLES = ("person", "organization", "product", "project", "contract")


def _candidate_strings(rows: list[dict]) -> list[str]:
    """rows の cell から canonical_name 候補となる string を集める。
    数値文字列・1 文字値は弾く。重複除去。"""
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        for v in r.values():
            if not isinstance(v, str):
                continue
            s = v.strip()
            if len(s) < 2:
                continue
            if s.replace(".", "").replace("-", "").isdigit():
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out[:50]


def _present_entity_tables(db: sqlite3.Connection) -> list[str]:
    """schema に定義された entity 表のうち実際に存在するものを返す。
    fixture によっては contract 等が migration されておらず欠ける場合がある。"""
    existing = {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return [t for t in _ENTITY_TABLES if t in existing and f"{t}_claims" in existing]


def sql_docs(rows: list[dict], k: int = 8) -> list[dict]:
    """rows の string 値を canonical_name として entity 表で逆引きし、
    対応する `*_claims.document_id` 経由で `documents.slug` を返す。

    LLM が出した SQL を parse せず、post-hoc に source doc を解決する。
    entity 名を含まない汎用 query (range 比較等) で FTS が doc を拾えない
    ケースのフォールバック。

    canonical_name claim は ingest で作られないため、column_name 条件は外し
    status='active' のみ。何らかの claim (org_type, founded_year 等) を
    持つ doc が「その entity を言及した doc」とみなされる。
    """
    cands = _candidate_strings(rows)
    if not cands:
        return []
    db = kg.connect(readonly=True)
    tables = _present_entity_tables(db)
    if not tables:
        return []
    placeholders = ",".join("?" * len(cands))
    parts = []
    for tbl in tables:
        parts.append(
            f"SELECT DISTINCT d.slug AS slug, e.canonical_name AS matched_value, "
            f"'{tbl}' AS entity_table "
            f"FROM {tbl}_claims c "
            f"JOIN {tbl} e ON e.id = c.{tbl}_id "
            f"JOIN documents d ON d.id = c.document_id "
            f"WHERE c.status = 'active' "
            f"  AND e.canonical_name IN ({placeholders})"
        )
    sql = " UNION ".join(parts) + f" LIMIT {k}"
    params = list(cands) * len(tables)
    try:
        return [dict(r) for r in db.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def answer_question(question: str) -> dict:
    llm.note("question", q=question)
    sql_used, error = None, None
    try:
        sql_used, rows = text2sql(question)
    except (ValueError, sqlite3.Error) as e:
        rows, error = [], f"text2sql 失敗: {e}"
        llm.note("text2sql.fallback", reason=str(e))
    docs = fts_docs(question)
    llm.note("fts.docs", n_docs=len(docs), slugs=[d.get("slug") for d in docs])
    sql_docs_ = sql_docs(rows) if rows else []
    llm.note("sql.docs", n_docs=len(sql_docs_),
             slugs=[d.get("slug") for d in sql_docs_])

    answer = ""
    try:
        prompt = (
            (config.PROMPTS / "answer.txt")
            .read_text(encoding="utf-8")
            .replace("{QUESTION}", question)
            .replace("{ROWS}", str(rows)[:4000] or "(なし)")
            .replace("{DOCS}", "\n".join(f"({d['slug']}) {d['snippet']}" for d in docs) or "(なし)")
        )
        with llm.ask_as("answer"):
            answer = llm.ask(prompt)
    except Exception as e:  # noqa: BLE001
        error = (error + " / " if error else "") + f"回答生成失敗: {e}"
        llm.note("answer.failed", error=str(e))

    return {
        "question": question,
        "sql": sql_used,
        "rows": rows,
        "docs": docs,
        "sql_docs": sql_docs_,
        "answer": answer,
        "error": error,
    }


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    res = answer_question(" ".join(args))
    if res["sql"]:
        print(f"[SQL] {res['sql']}\n")
    if res["error"]:
        print(f"[警告] {res['error']}\n")
    print(res["answer"] or "(回答生成不可)")


if __name__ == "__main__":
    main()
