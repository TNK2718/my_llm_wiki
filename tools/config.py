"""共通設定。環境に合わせてここだけ調整すれば全スクリプトに反映される。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Ollama ---
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma4:e2b"           # エッジ向け 2.3B 実効パラメータ。VRAM 少なめでも動く。
NUM_CTX = 8192                 # モデルのコンテキスト長に合わせる
TEMPERATURE = 0.2              # 保守係なので低め

# --- パス ---
RAW_SOURCES = ROOT / "raw" / "sources"
RAW_EXTRACTED = ROOT / "raw" / "extracted"
WIKI = ROOT / "wiki"
PROMPTS = ROOT / "prompts"
KG_DB = ROOT / "data" / "kg.sqlite"           # ナレッジグラフ（正本）
SCHEMA_SQL = ROOT / "tools" / "schema.sql"

# --- 分割要約の閾値（おおよその文字数。SLM のコンテキストに合わせて小さめ）---
CHUNK_CHARS = 6000             # これを超える抽出は分割して map-reduce 要約

# --- ダッシュボード ---
WEB_DIR = ROOT / "web"
HOST = "127.0.0.1"
PORT = 8000

# --- 重複・矛盾管理のルール設定 ---
# 正規化時に除去する法人格・敬称など（日本語/英語）
STRIP_TOKENS = [
    "株式会社", "有限会社", "合同会社", "（株）", "(株)", "㈱",
    "inc", "inc.", "corp", "corp.", "corporation", "co.", "co", "ltd",
    "ltd.", "llc", "k.k.", "kk",
]
# 値が1つに定まるべき述語（複数値が来たら矛盾候補にする）
FUNCTIONAL_PREDICATES = {"CEO", "代表者", "本社所在地", "設立年", "親会社"}
# 突合候補とみなすトリグラム類似のしきい値（0-1, 高いほど厳格）
DEDUP_SIM_THRESHOLD = 0.55
