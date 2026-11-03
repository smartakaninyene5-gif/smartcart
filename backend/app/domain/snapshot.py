"""Canonical snapshot hashing and material-change detection.

``snapshot_hash`` binds an approval to exactly what is being bought. The hash is
computed over a projection that excludes database row ids, so two quotes with
identical purchase content hash identically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel

from app.domain.models import MaterialChange, Quote, as_utc
from app.domain.money import CENT

SNAPSHOT_PREFIX = "sha256:"


def _normalise(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalise(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _normalise(val) for key, val in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, Decimal):
        return format(value.quantize(CENT), "f")
    if isinstance(value, datetime):
        return as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bool) or isinstance(value, str) or isinstance(value, int) or value is None:
        return value
    if isinstance(value, float):
        return format(Decimal(repr(value)).quantize(CENT), "f")
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _normalise(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def material_projection(quote: Quote) -> dict[str, Any]:
    """Everything that changes what is bought, and nothing that does not."""

    return {
        "user_id": quote.user_id,
        "currency": quote.currency,
        "policy_version": quote.policy_version,
        "requirements_version": quote.requirements.version,
        "currency_policy": quote.requirements.currency,
        "as_of": quote.as_of,
        "lines": [
            {
                "offer_id": line.offer_id,
                "product_id": line.product_id,
                "offer_product_id": line.offer_product_id,
                "variant_id": line.variant_id,
                "merchant_domain": line.merchant_domain,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "shipping_cost": line.shipping_cost,
                "currency": line.currency,
                "condition": line.condition,
                "availability": line.availability,
                "as_of": line.as_of,
                "evidence_ids": list(line.evidence_ids),
            }
            for line in quote.lines
        ],
        "subtotal": quote.subtotal,
        "shipping_total": quote.shipping_total,
        "grand_total": quote.grand_total,
        "evidence_refs": list(quote.evidence_refs),
    }


def compute_snapshot_hash(quote: Quote) -> str:
    digest = hashlib.sha256(canonical_json(material_projection(quote)).encode("utf-8")).hexdigest()
    return f"{SNAPSHOT_PREFIX}{digest}"


def seal(quote: Quote) -> Quote:
    """Return a copy carrying the hash of its own material projection."""

    return quote.model_copy(update={"snapshot_hash": compute_snapshot_hash(quote)})


def snapshot_hashes_match(quote: Quote) -> bool:
    return quote.snapshot_hash == compute_snapshot_hash(quote)


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
    else:
        flat[prefix] = node
    return flat


def diff_material(
    before: Quote | dict[str, Any],
    after: Quote | dict[str, Any],
    *,
    ignore_fields: tuple[str, ...] = (),
) -> list[MaterialChange]:
    """Field-level differences between two materially-relevant snapshots.

    ``ignore_fields`` lets revalidation ignore provenance-only movement
    (evidence ids, observation timestamps) while still refusing any change to
    price, quantity, merchant, condition or availability.
    """

    old = material_projection(before) if isinstance(before, Quote) else before
    new = material_projection(after) if isinstance(after, Quote) else after
    old_flat = _flatten(old)
    new_flat = _flatten(new)
    changes: list[MaterialChange] = []
    for path in sorted(set(old_flat) | set(new_flat)):
        if any(token in path for token in ignore_fields):
            continue
        old_value = old_flat.get(path)
        new_value = new_flat.get(path)
        if old_value != new_value:
            changes.append(MaterialChange(path=path, before=old_value, after=new_value))
    return changes


def has_material_change(before: Quote | dict[str, Any], after: Quote | dict[str, Any]) -> bool:
    return bool(diff_material(before, after))


def json_safe(value: Any) -> Any:
    """JSON-serialisable mirror of :func:`_normalise`.

    Use this before storing a structure into a JSON column. It applies the
    exact same conventions as :func:`canonical_json` (Decimals as fixed 2-dp
    strings, datetimes as UTC ISO strings, enums as their values), so

    ``canonical_json(json_safe(payload)) == canonical_json(payload)``

    holds and hash-chain content is unaffected.
    """

    return _normalise(value)


def chain_hash(prev_hash: str, payload_json: str) -> str:
    """Hash-chain entry for the append-only journal."""

    return hashlib.sha256(f"{prev_hash}\n{payload_json}".encode("utf-8")).hexdigest()


def payload_digest(payload: Any) -> str:
    return f"{SNAPSHOT_PREFIX}{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def digests_equal(a: str, b: str) -> bool:
    return hashlib.compare_digest(a, b) if hasattr(hashlib, "compare_digest") else a == b


def evidence_ids_for_offers(offers: Iterable[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(o.evidence_id for o in offers if getattr(o, "evidence_id", None)))
