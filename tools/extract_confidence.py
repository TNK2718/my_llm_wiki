"""logprob ベースの抽出 confidence 算出。

LLM の自己申告 confidence は校正されていない (gemma4:e2b では 0.85-0.95 plateau)。
代わりに同じ抽出コール内で取得した token logprobs を用い、各 entity / relation の
値スパンの logprob を集約 (`exp(mean(logprob))`) して confidence を埋め直す。

呼び出し元 (`ingest.extract_graph_from_chunks`) は:
  parsed, content_text, lp = llm.ask_json_with_logprobs(prompt)
  g = parse_extraction(parsed)
  g = apply_logprob_confidence(g, content_text, lp)
の順に使う。

不変条件: OpenAI 仕様で `lp[i].bytes` を順に concat すると `content_text` の UTF-8
バイト列と一致する。これを利用して各 token の (byte_start, byte_end) を計算し、
各値の JSON 文字列リテラル位置と重ねる。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import mean

from extract_schema import (
    EntityExtraction,
    GraphExtraction,
    RelationExtraction,
    WeakRelationExtraction,
)


# span 抽出失敗時の confidence。staging 行きの低値 (LOW_CONFIDENCE_THRESHOLD=0.3 を下回る)
FALLBACK_CONFIDENCE = 0.1


@dataclass(slots=True)
class LogprobToken:
    token: str
    logprob: float
    # OpenAI 仕様: UTF-8 bytes の int list。None の場合あり (model によっては未提供)
    bytes_: list[int] | None = None


def _token_byte_spans(tokens: list[LogprobToken]) -> list[tuple[int, int]]:
    """各 token の (byte_start, byte_end) を累積で返す。bytes が None の token は
    token 文字列を UTF-8 encode して長さを推定する。"""
    out: list[tuple[int, int]] = []
    pos = 0
    for t in tokens:
        if t.bytes_ is not None:
            length = len(t.bytes_)
        else:
            length = len(t.token.encode("utf-8"))
        out.append((pos, pos + length))
        pos += length
    return out


def _locate_value_span(
    content_bytes: bytes, value: str, cursor: list[int],
) -> tuple[int, int] | None:
    """content_bytes 上で `"<value>"` (両端 `"` 込み) を探し、内側 bytes 範囲を返す。

    両端 `"` を needle に含めることで、別フィールドの値 (evidence 等) の中に
    raw substring として value が出るケース (例: evidence="...大阪本社が東京...")
    での誤マッチを排除する。JSON 内で `"<value>"` という連続は基本フィールド
    境界にしか出ない。

    `ensure_ascii=False` の dump で失敗したら `ensure_ascii=True` (Unicode escape
    形式 `\\u00e9` 等) でフォールバック。LLM が `\\u00e9` を直接出してくる場合に
    対応するため。

    cursor[0] は呼び出し間で共有される前進専用オフセット。閉じ `"` の次まで進める。
    """
    if not value:
        return None
    for ensure_ascii in (False, True):
        dumped = json.dumps(value, ensure_ascii=ensure_ascii)
        if len(dumped) < 2 or dumped[0] != '"' or dumped[-1] != '"':
            continue
        quoted = dumped.encode("utf-8")
        if len(quoted) <= 2:
            continue
        idx = content_bytes.find(quoted, cursor[0])
        if idx < 0:
            continue
        inner_start = idx + 1
        inner_end = idx + len(quoted) - 1
        cursor[0] = idx + len(quoted)  # 閉じ `"` の次まで進める
        return (inner_start, inner_end)
    return None


def _tokens_in_span(
    span: tuple[int, int],
    byte_spans: list[tuple[int, int]],
    tokens: list[LogprobToken],
) -> list[LogprobToken]:
    """span (byte_start, byte_end) と重なる token を返す。境界 token も含める。"""
    a, b = span
    out: list[LogprobToken] = []
    for (ts, te), t in zip(byte_spans, tokens):
        if te > a and ts < b:
            out.append(t)
    return out


def _confidence_from_tokens(tokens: list[LogprobToken]) -> float:
    if not tokens:
        return FALLBACK_CONFIDENCE
    return math.exp(mean(t.logprob for t in tokens))


def _collect_confidence(
    values: list[str],
    content_bytes: bytes,
    byte_spans: list[tuple[int, int]],
    tokens: list[LogprobToken],
    cursor: list[int],
) -> float:
    """複数 value (relation の from+to, weak の subject+predicate+object) を
    1 つの span 集合として扱い、logprob を結合して confidence を出す。"""
    collected: list[LogprobToken] = []
    for v in values:
        span = _locate_value_span(content_bytes, v, cursor)
        if span is None:
            continue
        collected.extend(_tokens_in_span(span, byte_spans, tokens))
    return _confidence_from_tokens(collected)


_SECTION_KEYS = ("entities", "relations", "weak_relations")


def apply_logprob_confidence(
    graph: GraphExtraction,
    content_text: str,
    tokens: list[LogprobToken],
    *,
    raw: dict | None = None,
) -> GraphExtraction:
    """各 entity / relation / weak_relation の confidence を logprob 由来値に上書き。

    in-place で graph を更新して返す (パイプ流儀)。span 抽出失敗時は
    FALLBACK_CONFIDENCE。

    `raw` を渡すと dict.keys() の insertion order で section を処理する。
    これにより LLM が `{"relations": [...], "entities": [...]}` のような順で
    出力しても、forward-only cursor が物理 JSON 順と一致して破綻しない。
    raw が無ければ entities → relations → weak_relations の固定順。
    """
    if not content_text or not tokens:
        for e in graph.entities:
            e.confidence = FALLBACK_CONFIDENCE
        for r in graph.relations:
            r.confidence = FALLBACK_CONFIDENCE
        for w in graph.weak_relations:
            w.confidence = FALLBACK_CONFIDENCE
        return graph

    content_bytes = content_text.encode("utf-8")
    byte_spans = _token_byte_spans(tokens)
    cursor: list[int] = [0]  # mutable shared offset

    if isinstance(raw, dict):
        order = [k for k in raw.keys() if k in _SECTION_KEYS]
        # dict に存在しないセクションは末尾に (graph 側に居れば fallback で処理)
        for k in _SECTION_KEYS:
            if k not in order:
                order.append(k)
    else:
        order = list(_SECTION_KEYS)

    for section in order:
        if section == "entities":
            for e in graph.entities:
                e.confidence = _collect_confidence(
                    [e.canonical_name], content_bytes, byte_spans, tokens, cursor,
                )
        elif section == "relations":
            for r in graph.relations:
                r.confidence = _collect_confidence(
                    [r.from_, r.to], content_bytes, byte_spans, tokens, cursor,
                )
        elif section == "weak_relations":
            for w in graph.weak_relations:
                w.confidence = _collect_confidence(
                    [w.subject, w.predicate, w.object],
                    content_bytes, byte_spans, tokens, cursor,
                )

    return graph
