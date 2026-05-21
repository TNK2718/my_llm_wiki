"""typed-schema gold ↔ pred 抽出結果の集合一致判定 (schema_version 2)。

GraphExtraction (extract_schema.py) を直接受け取る前提:
  - entities[].canonical_name + proposed_type
  - relations[].proposed_junction + from + to
  - weak_relations[].subject + predicate + object
"""
from __future__ import annotations

from normalize import normalize


def _norm(s) -> str:
    return normalize(s if isinstance(s, str) else (str(s) if s is not None else ""))


# ---------- entity ----------
def entity_keys(items: list[dict]) -> set[tuple[str, str]]:
    """{(norm_canonical_name, proposed_type)} の集合。"""
    out: set[tuple[str, str]] = set()
    for e in items or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if nm and et:
            out.add((_norm(nm), et))
    return out


def gold_entity_alias_groups(gold: list[dict]) -> list[tuple[set[str], str]]:
    """gold 側で「同一 entity とみなす norm_name 集合」 + proposed_type。"""
    groups: list[tuple[set[str], str]] = []
    for e in gold or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if not nm or not et:
            continue
        keys = {_norm(nm)}
        for a in e.get("aliases") or []:
            if a:
                keys.add(_norm(a))
        groups.append((keys, et))
    return groups


def match_entities(gold: list[dict], pred: list[dict]) -> dict:
    pred_keys = entity_keys(pred)
    groups = gold_entity_alias_groups(gold)
    matched_group_idx: set[int] = set()
    matched_pred: set[tuple[str, str]] = set()
    for pk in pred_keys:
        nm, et = pk
        for i, (g_keys, g_et) in enumerate(groups):
            if et == g_et and nm in g_keys:
                matched_group_idx.add(i)
                matched_pred.add(pk)
                break
    missed = [
        {"canonical_name": gold[i].get("canonical_name") or gold[i].get("name"),
         "proposed_type": gold[i].get("proposed_type") or gold[i].get("type"),
         "aliases": gold[i].get("aliases") or []}
        for i in range(len(groups)) if i not in matched_group_idx
    ]
    extra = [{"canonical_name": nm, "proposed_type": et} for (nm, et) in (pred_keys - matched_pred)]
    return {
        "tp": len(matched_group_idx),
        "fp": len(pred_keys) - len(matched_pred),
        "fn": len(groups) - len(matched_group_idx),
        "missed": missed,
        "extra": extra,
    }


# ---------- relation (typed junction) ----------
def relation_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for r in items or []:
        j = r.get("proposed_junction")
        f = r.get("from") or r.get("subject")
        t = r.get("to") or r.get("object")
        if j and f and t:
            out.add((j, _norm(f), _norm(t)))
    return out


def match_relations(gold: list[dict], pred: list[dict]) -> dict:
    g = relation_keys(gold)
    p = relation_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"proposed_junction": j, "from": f, "to": t} for (j, f, t) in (g - p)],
        "extra": [{"proposed_junction": j, "from": f, "to": t} for (j, f, t) in (p - g)],
    }


# ---------- attribute claim (旧 facts 相当) ----------
def _gold_alias_to_canonical(gold: list[dict]) -> dict[tuple[str, str], str]:
    """gold の (norm_alias_or_canonical, type) → norm_canonical_name の lookup。

    pred 側 entity 名が gold の alias でしか出てこない場合 (例: pred="DPA",
    gold canonical="データ処理補遺", aliases=["DPA"]) に attribute を gold 側
    canonical へ寄せるために使う。
    """
    out: dict[tuple[str, str], str] = {}
    for e in gold or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if not nm or not et:
            continue
        canon = _norm(nm)
        out[(canon, et)] = canon
        for a in e.get("aliases") or []:
            if a:
                out[(_norm(a), et)] = canon
    return out


def attribute_keys(
    entities: list[dict],
    alias_to_canonical: dict[tuple[str, str], str] | None = None,
) -> set[tuple[str, str, str, str]]:
    """{(norm_canonical_name, proposed_type, column_name, norm_value)}.

    alias_to_canonical が与えられた場合、pred 側 entity 名を gold の canonical
    へ寄せてから keying する (entity 名のゆれを吸収)。
    """
    out: set[tuple[str, str, str, str]] = set()
    for e in entities or []:
        nm = e.get("canonical_name") or e.get("name")
        et = e.get("proposed_type") or e.get("type")
        if not nm or not et:
            continue
        nk = _norm(nm)
        if alias_to_canonical is not None:
            nk = alias_to_canonical.get((nk, et), nk)
        for col, val in (e.get("attributes") or {}).items():
            if val is None or val == "":
                continue
            out.add((nk, et, col, _norm(val)))
    return out


def match_facts(gold: list[dict], pred: list[dict]) -> dict:
    """entity.attributes として gold/pred を比較 (partial-gold + alias-aware)。

    - partial-gold: gold に登場した (entity, type, column) のセルのみ採点対象。
      それ以外の column を pred が出していても FP に算入しない (schema 制約外の
      attribute は ingest 側で staging に流れるため eval ノイズとして除外)。
    - alias-aware: pred 側 entity 名は gold の aliases を解決してから比較する。
      gold の entity 名と pred の表記ゆれ (例: "DPA" vs "データ処理補遺") を吸収。
    """
    alias2canon = _gold_alias_to_canonical(gold)
    g = attribute_keys(gold)
    p_all = attribute_keys(pred, alias_to_canonical=alias2canon)
    g_cells = {(n, t, c) for (n, t, c, _v) in g}
    p = {(n, t, c, v) for (n, t, c, v) in p_all if (n, t, c) in g_cells}
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"entity": n, "type": t, "column": c, "value": v} for (n, t, c, v) in (g - p)],
        "extra": [{"entity": n, "type": t, "column": c, "value": v} for (n, t, c, v) in (p - g)],
    }


# ---------- weak relations ----------
def weak_relation_keys(items: list[dict]) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for w in items or []:
        s, p, o = w.get("subject"), w.get("predicate"), w.get("object")
        if s and p and o:
            out.add((_norm(s), p, _norm(o)))
    return out


def match_weak_relations(gold: list[dict], pred: list[dict]) -> dict:
    g = weak_relation_keys(gold)
    p = weak_relation_keys(pred)
    return {
        "tp": len(g & p),
        "fp": len(p - g),
        "fn": len(g - p),
        "missed": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (g - p)],
        "extra": [{"subject": s, "predicate": pr, "object": o} for (s, pr, o) in (p - g)],
    }
