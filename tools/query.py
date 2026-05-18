"""質問応答。text2sql で構造化取得 → 自己修復 → fallback (FTS のみ)。

CLI:
  python tools/query.py "Acme の CEO は誰？"

サーバからは answer_question() を呼ぶ。
"""
import re
import sqlite3
import sys

import config
import llm
import db as kg


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


def text2sql(question: str):
    tmpl = (config.PROMPTS / "text2sql.txt").read_text(encoding="utf-8")
    sql = re.sub(r"```sql|```", "", llm.ask(tmpl.replace("{QUESTION}", question))).strip()
    try:
        sql = validate_sql(sql)
        return sql, run_ro(sql)
    except (ValueError, sqlite3.Error) as e1:
        fix = llm.ask(
            tmpl.replace("{QUESTION}", question) + f"\n\n直前の SQL はエラー: {e1}\n修正後の SQL のみ:"
        )
        sql = validate_sql(re.sub(r"```sql|```", "", fix).strip())
        return sql, run_ro(sql)


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


def answer_question(question: str) -> dict:
    """構造化結果・SQL・文書・回答をまとめて返す（サーバ/CLI 共用）。"""
    sql_used, route, error = None, "text2sql", None
    try:
        sql_used, rows = text2sql(question)
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
    if not args:
        print(__doc__)
        return
    res = answer_question(" ".join(args))
    if res["sql"]:
        print(f"[SQL/{res['route']}] {res['sql']}\n")
    if res["error"]:
        print(f"[警告] {res['error']}\n")
    print(res["answer"] or "(回答生成不可)")


if __name__ == "__main__":
    main()
