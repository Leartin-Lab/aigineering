"""Pure structural checks for claim provenance and evidence bindings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

MAX_ITEMS = 256
MAX_TEXT_BYTES = 1 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 16384


def _result(check: str, passed: bool, findings: set[str]) -> dict:
    return {
        "check": check,
        "passed": passed and not findings,
        "findings": "\n".join(sorted(findings)),
        "semantic_checked": False,
    }


def _bounded(
    value: object,
    total: list[int],
    *,
    depth: int = 0,
    nodes: list[int] | None = None,
    active: set[int] | None = None,
) -> str | None:
    nodes = [0] if nodes is None else nodes
    active = set() if active is None else active
    nodes[0] += 1
    if nodes[0] > MAX_NODES:
        return "input-too-many-nodes"
    if depth > MAX_DEPTH:
        return "input-too-deep"
    if isinstance(value, str):
        total[0] += len(value.encode("utf-8"))
        return None if total[0] <= MAX_TEXT_BYTES else "input-too-large"
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            return "cyclic-input"
        active.add(marker)
        try:
            for key, item in value.items():
                failure = _bounded(
                    key, total, depth=depth + 1, nodes=nodes, active=active
                ) or _bounded(item, total, depth=depth + 1, nodes=nodes, active=active)
                if failure:
                    return failure
        finally:
            active.remove(marker)
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        marker = id(value)
        if marker in active:
            return "cyclic-input"
        active.add(marker)
        try:
            for item in value:
                failure = _bounded(
                    item, total, depth=depth + 1, nodes=nodes, active=active
                )
                if failure:
                    return failure
        finally:
            active.remove(marker)
        return None
    return None


def _records(value: object, field: str, findings: set[str]) -> list[Mapping] | None:
    if not isinstance(value, list):
        findings.add(f"malformed-{field}")
        return None
    if not value:
        findings.add(f"empty-{field}")
        return None
    if len(value) > MAX_ITEMS:
        findings.add(f"too-many-{field}")
        return None
    if not all(isinstance(item, Mapping) for item in value):
        findings.add(f"malformed-{field}-record")
        return None
    return value


def _unique_text_ids(
    records: list[Mapping] | None,
    *,
    field: str,
    id_key: str,
    text_key: str,
    findings: set[str],
) -> dict[str, str] | None:
    if records is None:
        return None
    result: dict[str, str] = {}
    for record in records:
        identifier = record.get(id_key)
        text = record.get(text_key)
        if not isinstance(identifier, str) or not identifier:
            findings.add(f"invalid-{field}-id")
            continue
        if not isinstance(text, str) or not text:
            findings.add(f"invalid-{field}-text")
            continue
        if identifier in result:
            findings.add(f"duplicate-{field}-id")
        else:
            result[identifier] = text
    return result


def _claim_ids(
    source: object, report: object, findings: set[str]
) -> tuple[dict, dict] | None:
    if not isinstance(source, Mapping) or not isinstance(report, Mapping):
        findings.add("malformed-input")
        return None
    source_claims = _records(source.get("claims"), "source-claims", findings)
    report_claims = _records(report.get("claims"), "report-claims", findings)
    if source_claims is None or report_claims is None:
        return None
    source_ids: dict[str, Mapping] = {}
    report_ids: dict[str, Mapping] = {}
    for record in source_claims:
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier:
            findings.add("invalid-source-claim-id")
        elif identifier in source_ids:
            findings.add("duplicate-source-claim-id")
        else:
            source_ids[identifier] = record
    for record in report_claims:
        identifier = record.get("claim_id")
        if not isinstance(identifier, str) or not identifier:
            findings.add("invalid-report-claim-id")
        elif identifier in report_ids:
            findings.add("duplicate-report-claim-id")
        else:
            report_ids[identifier] = record
    if set(source_ids) != set(report_ids):
        findings.add("claim-id-set-mismatch")
    return source_ids, report_ids


def check_original_text(source: object, report: object) -> dict:
    """Check exact claim identity and original Unicode text preservation."""
    findings: set[str] = set()
    try:
        total = [0]
        bounded_failure = _bounded(source, total) or _bounded(report, total)
        if bounded_failure:
            return _result("original-text-v1", False, {bounded_failure})
        pair = _claim_ids(source, report, findings)
        if pair is not None:
            source_ids, report_ids = pair
            for identifier in sorted(set(source_ids) & set(report_ids)):
                source_text = source_ids[identifier].get("text")
                report_text = report_ids[identifier].get("original_text")
                if not isinstance(source_text, str) or not source_text:
                    findings.add("invalid-source-claim-text")
                if not isinstance(report_text, str) or not report_text:
                    findings.add("invalid-report-original-text")
                elif isinstance(source_text, str) and report_text != source_text:
                    findings.add(f"original-text-mismatch:{identifier}")
    except (RecursionError, UnicodeError, TypeError, ValueError):
        findings.add("malformed-input")
    return _result("original-text-v1", not findings, findings)


def check_evidence_bindings(source: object, report: object) -> dict:
    """Check exact excerpt IDs and literal substring evidence bindings."""
    findings: set[str] = set()
    try:
        total = [0]
        bounded_failure = _bounded(source, total) or _bounded(report, total)
        if bounded_failure:
            return _result("evidence-bindings-v1", False, {bounded_failure})
        pair = _claim_ids(source, report, findings)
        if not isinstance(source, Mapping) or not isinstance(report, Mapping):
            return _result("evidence-bindings-v1", False, findings)
        excerpts = _unique_text_ids(
            _records(source.get("source_excerpts"), "source-excerpts", findings),
            field="source-excerpt",
            id_key="id",
            text_key="text",
            findings=findings,
        )
        if excerpts is None or pair is None:
            return _result("evidence-bindings-v1", False, findings)
        _, report_ids = pair
        for identifier in sorted(report_ids):
            claim = report_ids[identifier]
            evidence_ids = claim.get("evidence_excerpt_ids")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                findings.add(f"missing-evidence-excerpt-ids:{identifier}")
                continue
            if len(evidence_ids) > MAX_ITEMS or any(
                not isinstance(item, str) or not item for item in evidence_ids
            ):
                findings.add(f"invalid-evidence-excerpt-ids:{identifier}")
                continue
            if len(set(evidence_ids)) != len(evidence_ids):
                findings.add(f"duplicate-evidence-excerpt-id:{identifier}")
            unknown = set(evidence_ids) - set(excerpts)
            if unknown:
                findings.add(f"unknown-evidence-excerpt-id:{identifier}")
            bindings = claim.get("evidence_bindings")
            if not isinstance(bindings, list) or not bindings:
                findings.add(f"missing-evidence-bindings:{identifier}")
                continue
            if len(bindings) > MAX_ITEMS:
                findings.add(f"too-many-evidence-bindings:{identifier}")
                continue
            binding_ids: list[str] = []
            seen: set[tuple[str, str]] = set()
            for binding in bindings:
                if not isinstance(binding, Mapping):
                    findings.add(f"malformed-evidence-binding:{identifier}")
                    continue
                excerpt_id, quote = binding.get("excerpt_id"), binding.get("quote")
                if not isinstance(excerpt_id, str) or not excerpt_id:
                    findings.add(f"invalid-binding-excerpt-id:{identifier}")
                    continue
                if not isinstance(quote, str) or not quote:
                    findings.add(f"invalid-binding-quote:{identifier}")
                    continue
                pair_key = (excerpt_id, quote)
                if pair_key in seen:
                    findings.add(f"duplicate-evidence-binding:{identifier}")
                seen.add(pair_key)
                binding_ids.append(excerpt_id)
                if excerpt_id not in excerpts:
                    findings.add(f"unknown-binding-excerpt-id:{identifier}")
                elif quote not in excerpts[excerpt_id]:
                    findings.add(f"quote-not-substring:{identifier}:{excerpt_id}")
            if set(binding_ids) != set(evidence_ids):
                findings.add(f"evidence-binding-set-mismatch:{identifier}")
    except (RecursionError, UnicodeError, TypeError, ValueError):
        findings.add("malformed-input")
    return _result("evidence-bindings-v1", not findings, findings)
