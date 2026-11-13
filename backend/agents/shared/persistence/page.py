from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Page(Generic[T]):
    """One cursor page. `next_key` is the raw DynamoDB key to resume from, or None on the last page."""

    items: list[T] = field(default_factory=list)
    next_key: dict[str, Any] | None = None


def from_dynamo(value: Any) -> Any:
    """Convert DynamoDB `Decimal` numbers to int/float so records validate and serialize as JSON."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value
