"""extract_confidence.apply_logprob_confidence の単体テスト。

token byte-offset → JSON 値 span → logprob mean → exp の経路を担保する。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from extract_confidence import (  # noqa: E402
    FALLBACK_CONFIDENCE,
    LogprobToken,
    _locate_value_span,
    _token_byte_spans,
    apply_logprob_confidence,
)
from extract_schema import (  # noqa: E402
    EntityExtraction,
    GraphExtraction,
    RelationExtraction,
    WeakRelationExtraction,
)


def _tokenize_chars(text: str, logprob: float = -0.5) -> list[LogprobToken]:
    """テスト用: 文字ごと 1 token に分割。bytes は UTF-8 encode で生成。"""
    out: list[LogprobToken] = []
    for ch in text:
        out.append(LogprobToken(
            token=ch, logprob=logprob, bytes_=list(ch.encode("utf-8")),
        ))
    return out


def test_token_byte_spans_accumulates_utf8_bytes():
    tokens = [
        LogprobToken("A", -0.1, bytes_=[0x41]),
        LogprobToken("あ", -0.2, bytes_=[0xE3, 0x81, 0x82]),
        LogprobToken("B", -0.3, bytes_=[0x42]),
    ]
    spans = _token_byte_spans(tokens)
    assert spans == [(0, 1), (1, 4), (4, 5)]
    # concat した bytes が text と一致する不変条件
    flat = bytes([b for t in tokens for b in t.bytes_])
    assert flat.decode("utf-8") == "AあB"


def test_locate_value_span_finds_canonical_in_dump():
    content = '{"name": "Alice", "city": "Tokyo"}'
    cursor = [0]
    span = _locate_value_span(content.encode("utf-8"), "Alice", cursor)
    assert span is not None
    a, b = span
    assert content.encode("utf-8")[a:b] == b"Alice"
    # cursor は閉じ quote の次まで進む (内側終端 + 1)
    assert cursor[0] == b + 1


def test_locate_value_span_advances_cursor_on_duplicate():
    content = '{"a": "Alice", "b": "Alice"}'
    cb = content.encode("utf-8")
    cursor = [0]
    s1 = _locate_value_span(cb, "Alice", cursor)
    s2 = _locate_value_span(cb, "Alice", cursor)
    assert s1 is not None and s2 is not None
    assert s1 != s2
    assert s1[1] <= s2[0]  # 2 回目は 1 回目より後ろ


def test_locate_value_span_handles_escaped_quote():
    raw_value = 'He said "hi"'
    dumped = json.dumps({"v": raw_value}, ensure_ascii=False)
    cursor = [0]
    span = _locate_value_span(dumped.encode("utf-8"), raw_value, cursor)
    assert span is not None
    # span が指すのは json.dumps が出した escape 済の内側 (\"hi\" 形式)
    inner = dumped.encode("utf-8")[span[0]:span[1]]
    assert inner == json.dumps(raw_value, ensure_ascii=False)[1:-1].encode("utf-8")


def test_apply_logprob_confidence_overwrites_self_reported():
    # content_text に entity の canonical_name が JSON 文字列として出現
    content = '{"entities": [{"proposed_type": "person", "canonical_name": "Alice"}]}'
    tokens = _tokenize_chars(content, logprob=-0.5)
    g = GraphExtraction(entities=[
        EntityExtraction(
            proposed_type="person", canonical_name="Alice", confidence=0.5,
        ),
    ])
    apply_logprob_confidence(g, content, tokens)
    # exp(mean([-0.5, ...])) ≈ exp(-0.5) ≈ 0.6065
    assert g.entities[0].confidence != 0.5  # 上書きされた
    assert math.isclose(g.entities[0].confidence, math.exp(-0.5), abs_tol=1e-6)


def test_apply_logprob_confidence_fallback_on_missing_span():
    # value が content_text に出現しない異常入力
    content = '{"entities": []}'
    tokens = _tokenize_chars(content)
    g = GraphExtraction(entities=[
        EntityExtraction(proposed_type="person", canonical_name="MissingName"),
    ])
    apply_logprob_confidence(g, content, tokens)
    assert g.entities[0].confidence == FALLBACK_CONFIDENCE


def test_locate_value_span_skips_substring_in_evidence():
    # entities[0].evidence の中に "大阪" が raw 文字として出現するが、
    # それは JSON 文字列の境界ではない (両端 `"` が無い) ので skip され、
    # entities[1].canonical_name の `"大阪"` にマッチする
    content = (
        '{"entities": ['
        '{"canonical_name": "東京", "evidence": "大阪本社が東京に移転"},'
        '{"canonical_name": "大阪"}'
        ']}'
    )
    cb = content.encode("utf-8")
    cursor = [0]
    # まず "東京" を消費して cursor 進める
    s_tokyo = _locate_value_span(cb, "東京", cursor)
    assert s_tokyo is not None
    # 続いて "大阪" — 旧実装なら evidence 内の `大阪本社` の `大阪` に hit したが、
    # 新実装は両端 quote 込みなので canonical_name の `"大阪"` だけにマッチする
    s_osaka = _locate_value_span(cb, "大阪", cursor)
    assert s_osaka is not None
    # 期待: canonical_name の値の位置 (evidence より後)
    canonical_osaka_pos = cb.find(b'"\xe5\xa4\xa7\xe9\x98\xaa"', cb.find(b'canonical_name', 50))
    # 内側は開き quote の次から始まる
    assert s_osaka[0] == canonical_osaka_pos + 1


def test_locate_value_span_ascii_escape_fallback():
    # LLM が `é` 形式でエスケープして出力するケース。content_text の bytes は
    # raw `é` (UTF-8 c3 a9) を含まず ASCII escape の `é` (8 bytes) を含む。
    # ensure_ascii=False の needle (`"café"`) では bytes 一致せず、ensure_ascii=True
    # の needle (`"café"`) にフォールバックして見つかる
    content = '{"name": "caf\\u00e9", "x": 1}'  # bytes 上は `café` (escape 形)
    cb = content.encode("utf-8")
    assert b"caf\\u00e9" in cb
    assert b"caf\xc3\xa9" not in cb  # raw UTF-8 の é は無い
    cursor = [0]
    span = _locate_value_span(cb, "café", cursor)
    assert span is not None
    # 内側 bytes は ascii escape された 8 bytes
    assert cb[span[0]:span[1]] == b"caf\\u00e9"


def test_apply_logprob_confidence_respects_raw_section_order():
    # LLM が relations を entities より先に出した場合でも、raw を渡せば
    # cursor が物理 JSON 順に従って進み、誤マッチしない
    content = '{"relations": [{"from": "Alice", "to": "Acme"}], "entities": [{"proposed_type": "person", "canonical_name": "Alice"}]}'
    raw = {  # LLM が出した dict (キー順は relations が先)
        "relations": [{"from": "Alice", "to": "Acme"}],
        "entities": [{"canonical_name": "Alice"}],
    }
    tokens = _tokenize_chars(content, logprob=-0.5)
    g = GraphExtraction(
        entities=[EntityExtraction(proposed_type="person", canonical_name="Alice")],
        relations=[RelationExtraction.model_validate({
            "proposed_junction": "employment", "from": "Alice", "to": "Acme",
        })],
    )
    apply_logprob_confidence(g, content, tokens, raw=raw)
    # どちらも logprob が見つかること (fallback 0.1 に落ちていない)
    assert g.entities[0].confidence > 0.1
    assert g.relations[0].confidence > 0.1


def test_apply_logprob_confidence_relation_concatenates_from_to():
    # from と to の両方を span にしてから mean を取る
    content = '{"relations": [{"from": "Alice", "to": "Acme"}]}'
    # Alice は logprob -0.4 (5 char), Acme は logprob -0.8 (4 char)
    tokens: list[LogprobToken] = []
    for ch in content:
        if ch in "Alice":
            lp = -0.4
        elif ch in "Acme":
            lp = -0.8
        else:
            lp = -2.0  # 周辺ノイズ
        # ただし "A" は Alice / Acme 両方に出るので位置依存にしたい → ここでは簡略化
        tokens.append(LogprobToken(token=ch, logprob=lp, bytes_=list(ch.encode("utf-8"))))
    g = GraphExtraction(relations=[
        RelationExtraction.model_validate({
            "proposed_junction": "employment", "from": "Alice", "to": "Acme",
        }),
    ])
    apply_logprob_confidence(g, content, tokens)
    # Alice (5 token, logprob ≈ -0.4) + Acme (4 token, logprob ≈ -0.8)
    # mean ≈ (5*-0.4 + 4*-0.8) / 9 ≈ -0.578
    # exp(-0.578) ≈ 0.561 → 単一値 (どちらか片方のみ) より低くなることを確認
    assert g.relations[0].confidence < math.exp(-0.4)
    assert g.relations[0].confidence > math.exp(-0.8)
