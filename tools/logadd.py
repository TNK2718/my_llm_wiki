"""log.md に追記する。プレフィックスを固定して grep 可能にする。
  grep "^## \\[" wiki/log.md | tail -5
"""
import sys
from datetime import date
import config


def add(kind: str, title: str, detail: str = ""):
    log = config.WIKI / "log.md"
    if not log.exists():
        log.write_text("# Log\n\n_append-only。手で並べ替えない。_\n\n", encoding="utf-8")
    entry = f"## [{date.today()}] {kind} | {title}\n"
    if detail:
        entry += detail.rstrip() + "\n"
    entry += "\n"
    with log.open("a", encoding="utf-8") as f:
        f.write(entry)


if __name__ == "__main__":
    # 使い方: python tools/logadd.py ingest "資料タイトル" "詳細"
    args = sys.argv[1:]
    add(args[0], args[1], args[2] if len(args) > 2 else "")
