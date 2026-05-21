"""Ollama への薄いラッパ。全モデル呼び出しはここを経由する。

trace_session() を with でくくると、その文脈で発生した ask()/embed() 呼び出しと
note() で記録した中間ステップを 1 本のタイムラインとして配列に集約する。
本番経路（server.py / CLI）からは使われない → 性能・挙動への影響なし。
"""
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar

import requests
import config


_trace: ContextVar[list | None] = ContextVar("_llm_trace", default=None)


@contextmanager
def trace_session():
    """ContextVar ベースの trace 開始。with ブロック内の ask/embed/note を 1 本に集約。"""
    sink: list = []
    token = _trace.set(sink)
    try:
        yield sink
    finally:
        _trace.reset(token)


def note(kind: str, **data) -> None:
    """構造化ステップを trace に追加。session 外なら no-op。"""
    sink = _trace.get()
    if sink is None:
        return
    entry = {"kind": kind, "t_ms": int(time.time() * 1000)}
    entry.update(data)
    sink.append(entry)


def _record_llm(kind: str, prompt: str, response: str | None, model: str,
                elapsed_ms: int, error: str | None = None, **extra) -> None:
    sink = _trace.get()
    if sink is None:
        return
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
    sink.append(entry)


def ask(prompt: str, system: str = "", temperature: float | None = None) -> str:
    """Ollama /api/generate を1回叩いて文字列を返す。1コール1タスクが原則。"""
    payload = {
        "model": config.MODEL,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": config.TEMPERATURE if temperature is None else temperature,
            "num_ctx": config.NUM_CTX,
        },
    }
    t0 = time.time()
    try:
        r = requests.post(config.OLLAMA_URL, json=payload, timeout=600)
        r.raise_for_status()
        out = r.json().get("response", "").strip()
        _record_llm(
            "llm.ask", prompt, out, config.MODEL,
            int((time.time() - t0) * 1000),
            system=system or None,
            temperature=payload["options"]["temperature"],
        )
        return out
    except Exception as e:  # noqa: BLE001
        _record_llm(
            "llm.ask", prompt, None, config.MODEL,
            int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
            system=system or None,
            temperature=payload["options"]["temperature"],
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
