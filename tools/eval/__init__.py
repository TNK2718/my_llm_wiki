"""精度評価パッケージ。

設計上の最重要不変条件:
  **本番 DB (data/kg.sqlite) には引数や環境に関わらず一切触れない。**

CLI エントリ (tools/eval/cli.py:main) が最初に config.KG_DB を eval 専用配下
(data/eval/_runtime/ または data/eval/fixtures/) へ書き換え、以降復元しない。
fixtures.assert_not_prod() を kg.connect 直前で必ず通すこと。
"""
from pathlib import Path
import sys as _sys

# 既存 tools/*.py は `import config` のような bare import を前提にしているため、
# tools/ ディレクトリを sys.path に通して相互運用性を確保する。
_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in _sys.path:
    _sys.path.insert(0, str(_TOOLS_DIR))
