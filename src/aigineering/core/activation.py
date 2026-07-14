"""Boolean activation expression evaluator for contract readiness."""

from __future__ import annotations

from typing import Optional

_MAX_DEPTH = 50
_MAX_TOKENS = 200


class NonMonotonicActivationError(ValueError):
    """Raised when an execution contract depends on fact absence."""


def _tokenize(expression: str) -> list[str]:
    tokens: list[str] = []
    for raw in expression.split():
        raw = raw.strip()
        if not raw:
            continue
        i = 0
        while i < len(raw):
            if raw[i] in "()":
                tokens.append(raw[i])
                i += 1
            else:
                j = i
                while j < len(raw) and raw[j] not in "()":
                    j += 1
                tokens.append(raw[i:j])
                i = j
    if len(tokens) > _MAX_TOKENS:
        raise ValueError(
            f"Activation expression too long: {len(tokens)} tokens (max {_MAX_TOKENS})"
        )
    return tokens


class _Parser:
    def __init__(self, tokens: list[str], available: set[str]) -> None:
        self._tokens = tokens
        self._pos = 0
        self._available = available
        self._depth = 0

    def evaluate(self) -> bool:
        if self._pos >= len(self._tokens):
            return True
        result = self._parse_or()
        if self._pos < len(self._tokens):
            raise ValueError(
                f"Unexpected token '{self._tokens[self._pos]}' after complete expression"
            )
        return result

    def _parse_or(self) -> bool:
        left = self._parse_and()
        while self._match("OR"):
            right = self._parse_and()
            left = left or right
        return left

    def _parse_and(self) -> bool:
        left = self._parse_not()
        while self._match("AND"):
            right = self._parse_not()
            left = left and right
        return left

    def _parse_not(self) -> bool:
        if self._match("NOT"):
            return not self._parse_not()
        return self._parse_primary()

    def _parse_primary(self) -> bool:
        if self._match("("):
            self._depth += 1
            if self._depth > _MAX_DEPTH:
                raise RecursionError(
                    f"Activation expression exceeds maximum nesting depth ({_MAX_DEPTH})"
                )
            value = self._parse_or()
            if not self._match(")"):
                raise ValueError("Missing closing ')'")
            self._depth -= 1
            return value
        return self._parse_name()

    def _parse_name(self) -> bool:
        if self._pos >= len(self._tokens):
            raise ValueError("Expected asset name but reached end of expression")
        token = self._tokens[self._pos]
        if token in ("AND", "OR", "NOT", "(", ")"):
            raise ValueError(f"Unexpected token '{token}' where asset name expected")
        self._pos += 1
        return token in self._available

    def _match(self, expected: str) -> bool:
        if self._pos < len(self._tokens) and self._tokens[self._pos] == expected:
            self._pos += 1
            return True
        return False


def check_activation(expression: Optional[str], available_names: set[str]) -> bool:
    if not expression or not expression.strip():
        return True
    tokens = _tokenize(expression)
    if not tokens:
        return True
    parser = _Parser(tokens, available_names)
    return parser.evaluate()


def validate_execution_activation(expression: Optional[str]) -> None:
    """Validate the monotonic activation subset accepted for execution.

    Runtime facts are append-only, so positive ``AND``/``OR`` predicates can
    only move from disabled to enabled. ``NOT`` depends on a closed-world
    absence assumption and can move in the opposite direction when a fact is
    appended; execution contracts therefore reject it at ingress.

    :func:`check_activation` retains ``NOT`` support for non-execution queries
    and compatibility with historical records.
    """

    if not expression or not expression.strip():
        return
    tokens = _tokenize(expression)
    if "NOT" in tokens:
        raise NonMonotonicActivationError(
            "execution activation must be monotonic: NOT/absence predicates "
            "are unsupported; model denial or cancellation as an explicit asset"
        )
    _Parser(tokens, set()).evaluate()
