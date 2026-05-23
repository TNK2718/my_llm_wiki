"""共通設定。環境に合わせてここだけ調整すれば全スクリプトに反映される。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --- Ollama ---
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"  # native chat (logprobs 対応・thinking 分離)
MODEL = "gemma4:e2b"           # エッジ向け 2.3B 実効パラメータ。VRAM 少なめでも動く。
NUM_CTX = 8192                 # モデルのコンテキスト長に合わせる
TEMPERATURE = 0.2              # 保守係なので低め
EXTRACT_TOP_LOGPROBS = 1       # ask_json_with_logprobs の top_logprobs パラメータ
TRACE_LOGPROBS = False         # True にすると trace に raw logprob_tokens を残す (debug 用)

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
# bootstrap で seed する __human__ document の id
HUMAN_DOCUMENT_ID = 1
# 同一 predicate が weak_relations に N 件超 蓄積したら schema_proposal を自動起票
WEAK_RELATION_PROMOTION_N = 5

# --- migrations ---
MIGRATIONS_DIR = ROOT / "tools" / "migrations"

# --- text2sql ヒント (attempt 1 から注入、attempt 2 でも再利用) ---
EMBED_URL = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text:latest"  # 768 dim, nomic-bert。軽量で安定。日本語類似は弱めなので閾値は低めに。
EMBED_CACHE_DB = ROOT / "data" / "embed_cache.sqlite"  # 値→ベクトルの永続キャッシュ
HINT_TRIGRAM_THRESHOLD = 0.20     # 突合(0.55)より緩く、recall 寄り。日本語同義語ペア (評価版↔トライアル版) は trigram で 0.1 前後しか出ないので、embed と組み合わせて拾う。
HINT_EMBED_THRESHOLD = 0.55       # cosine 類似。意味的に近い同義語を拾う想定。embed model 変更時は要再キャリブ。
HINT_TOPK_PER_COLUMN = 3
HINT_VOCAB_CAP = 12
HINT_MAX_CHARS = 700              # 8192 ctx 圧迫防止

# --- text2sql Fewshot 動的注入 ---
# 質問と類似する (NL, SQL) ペアを embedding cos-sim で top-k 取って prompt に注入する。
# プールが空 / 不在ならフォールバック (例なし)、embed 失敗時は先頭 k 件決定論的に。
FEWSHOT_POOL = ROOT / "data" / "fewshot" / "text2sql.yml"
FEWSHOT_TOPK = 3

# --- text2sql スキーマブロック動的生成 ---
# Core 表 (documents + entity canonical + relation junction) は常時注入、
# それ以外 (aliases / claims / existence / weak_relations 等) は質問との
# embedding cos-sim で top-K 選択する。schema_docs.yml の role 文が embed 対象。
SCHEMA_DOCS = ROOT / "data" / "schema_docs.yml"
SCHEMA_EXTRA_TOPK = 5

# --- entity matching (db.candidate_entities_*) ---
# typed-schema-design §D1 の「決定論的算出 (norm_key + embedding cos sim)」を実装する閾値。
# text2sql の HINT_* は recall 寄り、こちらは誤マージ防止のため precision 寄りで高めに設定。
ENTITY_TRIGRAM_THRESHOLD = 0.30   # candidate_entities_by_similarity(min_sim) のデフォルト
ENTITY_EMBED_THRESHOLD = 0.70     # cosine 類似度を「候補として採用」するしきい値
ENTITY_EMBED_ENABLED = True       # 緊急時に trigram + norm_key だけに戻すための feature flag
