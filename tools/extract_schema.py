"""LLM extract 出力の検証と confidence cap。

docs/typed-schema-design.md §5「LLM-extracted claim の confidence は extractor で
min(raw_extracted_conf, LLM_CONFIDENCE_CAP) に cap」を実装する。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

import config


EntityType = Literal["person", "organization", "product", "project"]
RelationJunction = Literal["employment", "manufacturing", "org_hierarchy"]


class ColumnSpec(BaseModel):
    name: str
    type: str = "TEXT"
    example: Optional[str] = None


class NewTableProposal(BaseModel):
    table: str
    extends_from: Optional[str] = None
    additional_columns: list[ColumnSpec] = Field(default_factory=list)
    rationale: Optional[str] = None


class NewJunctionProposal(BaseModel):
    junction: str
    from_type: Optional[str] = None
    to_type: Optional[str] = None
    additional_columns: list[ColumnSpec] = Field(default_factory=list)
    rationale: Optional[str] = None


class ExistingMatchHint(BaseModel):
    """LLM が「この entity は既存 <table> の <name> と紛らわしい」と提案する任意ヒント。

    類似度スコアは持たない（pipeline 側で決定論的に算出する）。
    """
    table: str
    name: str
    rationale: Optional[str] = None


class EntityExtraction(BaseModel):
    proposed_type: str  # starter or arbitrary (proposal kind)
    canonical_name: str
    mention_surface: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.5
    evidence: Optional[str] = None
    new_table_proposal: Optional[NewTableProposal] = None
    existing_matches: list[ExistingMatchHint] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _cap_confidence(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            return 0.5
        return min(max(float(v), 0.0), config.LLM_CONFIDENCE_CAP)


class RelationExtraction(BaseModel):
    proposed_junction: str
    from_: str = Field(alias="from")
    to: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: Optional[str] = None
    confidence: float = 0.5
    new_junction_proposal: Optional[NewJunctionProposal] = None

    model_config = {"populate_by_name": True}

    @field_validator("confidence")
    @classmethod
    def _cap_confidence(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            return 0.5
        return min(max(float(v), 0.0), config.LLM_CONFIDENCE_CAP)


class WeakRelationExtraction(BaseModel):
    subject: str
    predicate: str
    object: str  # entity name または scalar
    evidence: Optional[str] = None
    confidence: float = 0.3

    @field_validator("confidence")
    @classmethod
    def _cap_confidence(cls, v: float) -> float:
        if not isinstance(v, (int, float)):
            return 0.3
        return min(max(float(v), 0.0), config.LLM_CONFIDENCE_CAP)


class GraphExtraction(BaseModel):
    entities: list[EntityExtraction] = Field(default_factory=list)
    relations: list[RelationExtraction] = Field(default_factory=list)
    weak_relations: list[WeakRelationExtraction] = Field(default_factory=list)


def parse_extraction(raw: Any) -> GraphExtraction:
    """LLM 出力 (dict/list) を GraphExtraction に整形。失敗時は空 graph を返す。"""
    if isinstance(raw, list):
        # 弱いモデルが entities 配列だけを返したケースの保険
        raw = {"entities": raw}
    if not isinstance(raw, dict):
        return GraphExtraction()
    # 旧キー (facts) が来た場合は無視 (新スキーマでは attributes に統合)
    payload = {
        "entities": raw.get("entities") or [],
        "relations": raw.get("relations") or [],
        "weak_relations": raw.get("weak_relations") or [],
    }
    try:
        return GraphExtraction.model_validate(payload)
    except ValidationError:
        # 個別 entity/relation を best-effort で拾う
        out = GraphExtraction()
        for e in payload["entities"]:
            try:
                out.entities.append(EntityExtraction.model_validate(e))
            except ValidationError:
                continue
        for r in payload["relations"]:
            try:
                out.relations.append(RelationExtraction.model_validate(r))
            except ValidationError:
                continue
        for w in payload["weak_relations"]:
            try:
                out.weak_relations.append(WeakRelationExtraction.model_validate(w))
            except ValidationError:
                continue
        return out
