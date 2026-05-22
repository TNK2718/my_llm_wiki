"""Ollama への薄いラッパ。全モデル呼び出しはここを経由する。

trace_session() を with でくくると、その文脈で発生した ask()/embed() 呼び出しと
note() で記録した中間ステップを 1 本のタイムラインとして配列に集約する。
本番経路（server.py / CLI）からは使われない → 性能・挙動への影響なし。

stream_session(emit) を with でくくると、同じイベントが emit(dict) でも逐次配信される。
さらに ask() は Ollama を stream=True で叩き、llm.ask.start / llm.delta を emit する。
非 stream session 下では完全に従来挙動。
"""
import hashlib
import json
import math
import sqlite3
import struct
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

import requests
import config


_trace: ContextVar[list | None] = ContextVar("_llm_trace", default=None)
_stream_emit: ContextVar[Callable[[dict], None] | None] = ContextVar(
    "_llm_stream_emit", default=None
)
_ask_purpose: ContextVar[str] = ContextVar("_llm_ask_purpose", default="")


@contextmanager
def trace_session():
    """ContextVar ベースの trace 開始。with ブロック内の ask/embed/note を 1 本に集約。"""
    sink: list = []
    token = _trace.set(sink)
    try:
        yield sink
    finally:
        _trace.reset(token)


@contextmanager
def stream_session(emit: Callable[[dict], None]):
    """note()/_record_llm()/ask() の chunk を emit(dict) でリアルタイム配信。

    SSE エンドポイントが worker thread 内でこれを set して使う。
    ContextVar はスレッドローカルなので必ず worker thread の中で with すること。
    """
    token = _stream_emit.set(emit)
    try:
        yield
    finally:
        _stream_emit.reset(token)


@contextmanager
def ask_as(purpose: str):
    """直後の llm.ask() に「どのステップの呼び出しか」のタグを付ける。

    streaming 時に llm.ask.start / llm.delta / llm.ask の purpose フィールドに反映され、
    frontend がこの purpose をキーに「どのカードに token を流すか」を決める。
    非 streaming 時は no-op。
    """
    token = _ask_purpose.set(purpose)
    try:
        yield
    finally:
        _ask_purpose.reset(token)


def _emit(ev: dict) -> None:
    cb = _stream_emit.get()
    if cb is None:
        return
    try:
        cb(ev)
    except Exception:  # noqa: BLE001 — UI 配信失敗で本処理を壊さない
        pass


def note(kind: str, **data) -> None:
    """構造化ステップを trace に追加。session 外なら no-op。"""
    entry = {"kind": kind, "t_ms": int(time.time() * 1000)}
    entry.update(data)
    sink = _trace.get()
    if sink is not None:
        sink.append(entry)
    _emit(entry)


def _record_llm(kind: str, prompt: str, response: str | None, model: str,
                elapsed_ms: int, error: str | None = None, **extra) -> None:
    entry = {
        "kind": kind,
        "t_ms": int(time.time() * 1000),
        "model": model,
        "elapsed_ms": elapsed_ms,
        "prompt": prompt,
        "response": response,
    }
    if error is not None:
        entry["error"] = error
    entry.update(extra)
    sink = _trace.get()
    if sink is not None:
        sink.append(entry)
    _emit(entry)


def ask(prompt: str, system: str = "", temperature: float | None = None) -> str:
    """Ollama /api/generate を1回叩いて文字列を返す。1コール1タスクが原則。

    stream_session() が active のときだけ Ollama を stream=True で叩き、chunk ごとに
    llm.delta を emit する。戻り値の文字列は両モードで同じ。
    """
    temp = config.TEMPERATURE if temperature is None else temperature
    purpose = _ask_purpose.get()
    streaming = _stream_emit.get() is not None
    payload = {
        "model": config.MODEL,
        "prompt": prompt,
        "system": system,
        "stream": streaming,
        "options": {
            "temperature": temp,
            "num_ctx": config.NUM_CTX,
        },
    }
    t0 = time.time()
    if streaming:
        _emit({
            "kind": "llm.ask.start",
            "t_ms": int(time.time() * 1000),
            "model": config.MODEL,
            "purpose": purpose,
        })
        parts: list[str] = []
        try:
            with requests.post(
                config.OLLAMA_URL, json=payload, timeout=600, stream=True
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=False):
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("response", "")
                    if delta:
                        parts.append(delta)
                        _emit({
                            "kind": "llm.delta",
                            "t_ms": int(time.time() * 1000),
                            "delta": delta,
                            "purpose": purpose,
                        })
                    if chunk.get("done"):
                        break
            out = "".join(parts).strip()
            _record_llm(
                "llm.ask", prompt, out, config.MODEL,
                int((time.time() - t0) * 1000),
                system=system or None,
                temperature=temp,
                purpose=purpose or None,
            )
            return out
        except Exception as e:  # noqa: BLE001
            _emit({
                "kind": "llm.error",
                "t_ms": int(time.time() * 1000),
                "purpose": purpose,
                "error": f"{type(e).__name__}: {e}",
            })
            _record_llm(
                "llm.ask", prompt, None, config.MODEL,
                int((time.time() - t0) * 1000),
                error=f"{type(e).__name__}: {e}",
                system=system or None,
                temperature=temp,
                purpose=purpose or None,
            )
            raise
    try:
        r = requests.post(config.OLLAMA_URL, json=payload, timeout=600)
        r.raise_for_status()
        out = r.json().get("response", "").strip()
        _record_llm(
            "llm.ask", prompt, out, config.MODEL,
            int((time.time() - t0) * 1000),
            system=system or None,
            temperature=temp,
        )
        return out
    except Exception as e:  # noqa: BLE001
        _record_llm(
            "llm.ask", prompt, None, config.MODEL,
            int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
            system=system or None,
            temperature=temp,
        )
        raise


def embed(text: str) -> list[float] | None:
    """Ollama /api/embeddings を 1 回叩く。失敗時は None で呼び出し側に縮退を委ねる。"""
    if not text:
        return None
    t0 = time.time()
    try:
        r = requests.post(
            config.EMBED_URL,
            json={"model": config.EMBED_MODEL, "prompt": text},
            timeout=60,
        )
        r.raise_for_status()
        v = r.json().get("embedding")
        ok = isinstance(v, list) and bool(v)
        _record_llm(
            "llm.embed", text[:200], None, config.EMBED_MODEL,
            int((time.time() - t0) * 1000),
            dim=(len(v) if ok else 0),
        )
        return v if ok else None
    except requests.RequestException as e:
        _record_llm(
            "llm.embed", text[:200], None, config.EMBED_MODEL,
            int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
        return None


# ---------- 永続キャッシュ付き embed + cosine ----------
# column_hints (query.py) と entity matching (db.py via ingest.py) の両方で同じ
# data/embed_cache.sqlite を共有する。cache key は SHA1(EMBED_MODEL|text)。
def _embed_cache_conn() -> sqlite3.Connection:
    config.EMBED_CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.EMBED_CACHE_DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS embed_cache("
        " key TEXT PRIMARY KEY, vec BLOB NOT NULL)"
    )
    return c


def embed_cached(text: str) -> list[float] | None:
    """embed() の永続キャッシュ版。Ollama 失敗時は None で縮退。"""
    if not text:
        return None
    key = hashlib.sha1(f"{config.EMBED_MODEL}|{text}".encode("utf-8")).hexdigest()
    c = _embed_cache_conn()
    try:
        row = c.execute("SELECT vec FROM embed_cache WHERE key=?", (key,)).fetchone()
        if row:
            blob = row[0]
            n = len(blob) // 4
            return list(struct.unpack(f"{n}f", blob))
        v = embed(text)
        if v is None:
            return None
        blob = struct.pack(f"{len(v)}f", *v)
        c.execute("INSERT OR REPLACE INTO embed_cache(key,vec) VALUES(?,?)", (key, blob))
        c.commit()
        return v
    finally:
        c.close()


def cosine(a: list[float], b: list[float]) -> float:
    """L2 正規化なしの cosine 類似度。空 / 長さ不一致 / zero norm は 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    s = sa = sb = 0.0
    for x, y in zip(a, b):
        s += x * y
        sa += x * x
        sb += y * y
    if sa <= 0 or sb <= 0:
        return 0.0
    return s / (math.sqrt(sa) * math.sqrt(sb))


def _parse_json_loose(raw: str):
    """コードフェンス除去 + bracket fallback 付きの JSON parse。失敗時は空 list。"""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # よくある失敗: 最初の [ ... ] だけ拾う
        s, e = raw.find("["), raw.rfind("]")
        if s != -1 and e != -1:
            try:
                return json.loads(raw[s : e + 1])
            except json.JSONDecodeError:
                pass
        # 続いて { ... } 抽出
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            try:
                return json.loads(raw[s : e + 1])
            except json.JSONDecodeError:
                pass
        return []


def ask_json(prompt: str, system: str = ""):
    """JSON のみを返させたい時用。失敗したら空 list を返す（弱いモデルの保険）。"""
    return _parse_json_loose(ask(prompt, system=system))


def _slice_content_tokens(content_text: str, tokens: list) -> list:
    """logprobs token 列から content_text 部分の token だけを切り出す。

    Ollama `/api/chat` の logprobs は thinking + content の token を順に含む
    (例: `<|channel>thought\\n...thinking...<channel|>{"json"}`)。
    content_text の UTF-8 bytes が token bytes 連結の末尾に出現する位置を rfind し、
    そこに重なる token のみ返す。
    """
    if not tokens or not content_text:
        return []
    target = content_text.encode("utf-8")
    parts: list[bytes] = []
    for t in tokens:
        if t.bytes_ is not None:
            parts.append(bytes(t.bytes_))
        else:
            parts.append(t.token.encode("utf-8"))
    flat = b"".join(parts)
    idx = flat.rfind(target)
    if idx < 0:
        return []
    end_byte = idx + len(target)
    out = []
    acc = 0
    for t, p in zip(tokens, parts):
        token_start = acc
        token_end = acc + len(p)
        # token と target が重なれば include
        if token_end > idx and token_start < end_byte:
            out.append(t)
        acc = token_end
        if token_start >= end_byte:
            break
    return out


def ask_json_with_logprobs(
    prompt: str,
    system: str = "",
    top_logprobs: int | None = None,
    temperature: float | None = None,
):
    """Ollama /api/chat で logprobs 付きで生成。

    戻り値: (parsed, content_text, logprob_tokens)
      - parsed: JSON parse 結果 (失敗時は空 list)
      - content_text: LLM の content 文字列 (thinking 除外済)
      - logprob_tokens: list[extract_confidence.LogprobToken]。
        thinking 部分は除外し、content_text の UTF-8 と bytes 連結が一致するスライス。

    `ask()` (`/api/generate`) は logprobs 未対応、`/v1/chat/completions` (OpenAI 互換)
    は長文 prompt で出力構造を壊すので、Ollama native `/api/chat` を使う。
    """
    from extract_confidence import LogprobToken  # 循環回避のため遅延 import

    top_n = config.EXTRACT_TOP_LOGPROBS if top_logprobs is None else top_logprobs
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": config.MODEL,
        "messages": messages,
        "options": {
            "temperature": config.TEMPERATURE if temperature is None else temperature,
            "num_ctx": config.NUM_CTX,
        },
        "logprobs": True,
        "top_logprobs": top_n,
        "stream": False,
    }
    t0 = time.time()
    try:
        r = requests.post(config.OLLAMA_CHAT_URL, json=payload, timeout=600)
        r.raise_for_status()
        data = r.json()
        message = data.get("message") or {}
        content_text = message.get("content") or ""
        raw_logprobs = data.get("logprobs") or []
        all_tokens: list[LogprobToken] = []
        for entry in raw_logprobs:
            all_tokens.append(LogprobToken(
                token=entry.get("token", ""),
                logprob=float(entry.get("logprob", 0.0)),
                bytes_=entry.get("bytes"),
            ))
        # thinking 部分を除外し content_text に対応する token のみ
        tokens = _slice_content_tokens(content_text, all_tokens)
        parsed = _parse_json_loose(content_text)
        extra: dict = {
            "n_tokens": len(tokens),
            "n_tokens_total": len(all_tokens),
            "top_logprobs": top_n,
        }
        if config.TRACE_LOGPROBS:
            extra["logprob_tokens"] = [
                {"token": t.token, "logprob": t.logprob, "bytes": t.bytes_}
                for t in tokens
            ]
        _record_llm(
            "llm.ask_json_with_logprobs", prompt, content_text, config.MODEL,
            int((time.time() - t0) * 1000),
            system=system or None,
            temperature=payload["options"]["temperature"],
            **extra,
        )
        return parsed, content_text, tokens
    except Exception as e:  # noqa: BLE001
        _record_llm(
            "llm.ask_json_with_logprobs", prompt, None, config.MODEL,
            int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
            system=system or None,
            temperature=payload.get("options", {}).get("temperature"),
        )
        raise
