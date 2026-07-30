"""Publish advisory semantic matches through the Candidate boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from aigineering.protocol.asset_graph import (
    create_definition_content_assertion,
)
from aigineering.protocol.effect_builders import (
    definition_content_assertion_effect,
)


@dataclass(frozen=True)
class SemanticMatch:
    """Auditable matcher output; never an identity or authority decision."""

    model: str
    model_version: str
    score: str
    threshold: str
    evidence_uri: str = ""

    def __post_init__(self) -> None:
        if not self.model or not self.model_version:
            raise ValueError("semantic match requires model and model_version")
        if not isinstance(self.score, str) or not isinstance(self.threshold, str):
            raise ValueError("semantic score and threshold must be decimal strings")
        try:
            score = Decimal(self.score)
            threshold = Decimal(self.threshold)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(
                "semantic score and threshold must be decimal strings"
            ) from exc
        if not score.is_finite() or not (Decimal("0") <= score <= Decimal("1")):
            raise ValueError("semantic score must be between 0 and 1")
        if not threshold.is_finite() or not (Decimal("0") <= threshold <= Decimal("1")):
            raise ValueError("semantic threshold must be between 0 and 1")
        if score < threshold:
            raise ValueError("semantic score does not meet the declared threshold")


def publish_semantic_relation(
    publisher,
    *,
    definition_id: str,
    content_id: str,
    match: SemanticMatch,
    idempotency_key: str,
):
    """Sign one assertion and publish it as an ordinary Candidate effect."""
    assertion = create_definition_content_assertion(
        domain_id=publisher.genesis.id,
        definition_id=definition_id,
        content_id=content_id,
        relation_type="semantic-equivalent",
        actor_id=publisher.actor_key.actor_id,
        key_id=publisher.actor_key.key_id,
        signer=publisher.signer,
        evidence={
            "evidence_uri": match.evidence_uri,
            "model": match.model,
            "model_version": match.model_version,
            "score": match.score,
            "threshold": match.threshold,
        },
    )
    return publisher.publish(
        (definition_content_assertion_effect(assertion),),
        idempotency_key=idempotency_key,
    )
