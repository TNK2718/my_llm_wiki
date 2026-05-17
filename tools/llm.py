"""Ollama への薄いラッパ。全モデル呼び出しはここを経由する。"""
import json
import requests
import config


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
    r = requests.post(config.OLLAMA_URL, json=payload, timeout=600)
    r.raise_for_status()
    return r.json().get("response", "").strip()


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
