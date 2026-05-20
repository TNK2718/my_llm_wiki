"""決定的な名前正規化と類似度。typed-schema 設計でも norm_key/aliases 突合に使う。"""
import re
import unicodedata

import config


def normalize(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower().strip()
    for tok in sorted(config.STRIP_TOKENS, key=len, reverse=True):
        s = s.replace(tok.lower(), "")
    s = re.sub(r"[\s　_,.\-・()（）]+", "", s)
    return s


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def similarity(a: str, b: str) -> float:
    """正規化キー同士の Jaccard トリグラム類似 (0-1)。"""
    if not a or not b:
        return 0.0
    ta, tb = _trigrams(a), _trigrams(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0
