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
PROMPTS = ROOT / "prompts"
KG_DB = ROOT / "data" / "kg.sqlite"           # ナレッジグラフ（正本）
LOG_PATH = ROOT / "data" / "log.md"           # 取り込み等の append-only ログ
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
# 突合候補とみなすトリグラム類似のしきい値（0-1, 高いほど厳格）
DEDUP_SIM_THRESHOLD = 0.55

# --- typed-schema claims 状態遷移 (docs/typed-schema-design.md §3, §5, §7) ---
# 新 claim が既存最高 active より δ 以上高ければ canonical を UPDATE、δ 以内なら conflict
CLAIM_CONFIDENCE_DELTA = 0.1
# conf < この値の claim は staging / weak_relations に振り分ける
LOW_CONFIDENCE_THRESHOLD = 0.3
# LLM 抽出 conf の上限。人手 verdict (1.0) が doc 主張に silent 降格されないよう cap
LLM_CONFIDENCE_CAP = 0.95
# bootstrap で seed する __human__ document の id
HUMAN_DOCUMENT_ID = 1
# 同一 predicate が weak_relations に N 件超 蓄積したら schema_proposal を自動起票
WEAK_RELATION_PROMOTION_N = 5

# --- migrations ---
MIGRATIONS_DIR = ROOT / "tools" / "migrations"

# --- text2sql 修復ヒント（失敗時のみ参照） ---
EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "bge-m3:latest"     # 多言語・日本語強め (1024 dim)。granite より重いが cache で吸収。
EMBED_CACHE_DB = ROOT / "data" / "embed_cache.sqlite"  # 値→ベクトルの永続キャッシュ
HINT_TRIGRAM_THRESHOLD = 0.20     # 突合(0.55)より緩く、recall 寄り。日本語同義語ペア (評価版↔トライアル版) は trigram で 0.1 前後しか出ないので、embed と組み合わせて拾う。
HINT_EMBED_THRESHOLD = 0.55       # cosine 類似。bge-m3 で意味的に近い同義語を 0.55+ で拾う想定。
HINT_TOPK_PER_COLUMN = 3
HINT_VOCAB_CAP = 12
HINT_MAX_CHARS = 700              # 8192 ctx 圧迫防止
