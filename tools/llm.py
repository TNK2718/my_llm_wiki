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


def ask_json(prompt: str, system: str = ""):
    """JSON のみを返させたい時用。失敗したら空 list を返す（弱いモデルの保険）。"""
    raw = ask(prompt, system=system)
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
        return []
