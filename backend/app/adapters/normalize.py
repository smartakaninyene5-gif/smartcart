"""Tolerant normalisation of provider payloads into domain objects.

Provider data is untrusted. Anything structurally unusable is skipped and
reported as a warning; nothing is invented. In particular ``condition: null``
becomes ``UNKNOWN`` and is never promoted to "new".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from app.domain.enums import Availability, Condition
from app.domain.models import Offer, Product, as_utc, normalise_domain, offer_from_raw
from app.domain.money import money
from app.ports import SourceMalformed

_REDIRECTOR_HOSTS = ("trychannel3.com",)


@dataclass
class NormalisedProduct:
    product: Product
    raw_product: Any
    raw_offers: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def parse_datetime(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return as_utc(datetime.fromisoformat(text))
        except ValueError:
            return as_utc(fallback)
    return as_utc(fallback)


def parse_condition(value: Any) -> Condition:
    if value is None:
        return Condition.UNKNOWN
    if isinstance(value, Condition):
        return value
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"", "unknown", "none", "null"}:
        return Condition.UNKNOWN
    if text in {"new", "brand_new", "sealed"}:
        return Condition.NEW
    if text in {"refurbished", "renewed", "refurb"}:
        return Condition.REFURBISHED
    if text in {"open_box", "openbox"}:
        return Condition.OPEN_BOX
    if text in {"used", "pre_owned", "preowned", "second_hand"}:
        return Condition.USED
    return Condition.UNKNOWN


def parse_availability(value: Any) -> Availability:
    if value is None:
        return Availability.UNKNOWN
    if isinstance(value, Availability):
        return value
    text = str(value).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    mapping = {
        "instock": Availability.IN_STOCK,
        "available": Availability.IN_STOCK,
        "in_stock": Availability.IN_STOCK,
        "outofstock": Availability.OUT_OF_STOCK,
        "unavailable": Availability.OUT_OF_STOCK,
        "preorder": Availability.PREORDER,
        "pre_order": Availability.PREORDER,
        "backorder": Availability.BACKORDER,
        "back_order": Availability.BACKORDER,
    }
    return mapping.get(text, Availability.UNKNOWN)


def _first_brand(payload: dict[str, Any]) -> str | None:
    brands = payload.get("brands")
    if isinstance(brands, list):
        for entry in brands:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"]).strip() or None
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
    if isinstance(brands, str) and brands.strip():
        return brands.strip()
    return None


def _image_url(payload: dict[str, Any]) -> str | None:
    images = payload.get("images")
    if not isinstance(images, list):
        return None
    ordered = sorted(
        (img for img in images if isinstance(img, dict) and img.get("url")),
        key=lambda img: 0 if img.get("is_main_image") else 1,
    )
    return str(ordered[0]["url"]) if ordered else None


def _category(payload: dict[str, Any]) -> str | None:
    category = payload.get("category")
    if isinstance(category, dict):
        return str(category.get("title") or category.get("slug") or "") or None
    if isinstance(category, str):
        return category.strip() or None
    return None


def _attributes(payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = payload.get("structured_attributes")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            cleaned = [str(item) for item in value if item is not None]
        elif value is None:
            cleaned = []
        else:
            cleaned = [str(value)]
        if cleaned:
            result[str(key)] = cleaned
    return result


def _selected_options(payload: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    variants = payload.get("variants")
    if not isinstance(variants, dict):
        return ()
    selected = variants.get("selected")
    if not isinstance(selected, list):
        return ()
    pairs: list[tuple[str, str]] = []
    for item in selected:
        if isinstance(item, dict) and item.get("name") and item.get("label"):
            pairs.append((str(item["name"]), str(item["label"])))
    return tuple(pairs)


def _offer_price(raw: dict[str, Any], default_currency: str) -> tuple[Decimal, str] | None:
    price = raw.get("price")
    if isinstance(price, dict):
        amount = price.get("price", price.get("amount"))
        currency = price.get("currency") or raw.get("currency") or default_currency
    else:
        amount = price
        currency = raw.get("currency") or default_currency
    if amount is None:
        return None
    try:
        return money(amount, "offer price", allow_zero=False), str(currency).upper()
    except ValueError:
        return None


def _offer_shipping(raw: dict[str, Any]) -> Decimal | None:
    value = raw.get("shipping_cost", raw.get("shipping"))
    if isinstance(value, dict):
        value = value.get("price", value.get("amount"))
    if value is None:
        return None
    try:
        return money(value, "shipping")
    except ValueError:
        return None


def _offer_shipping_currency(raw: dict[str, Any]) -> str | None:
    value = raw.get("shipping")
    if isinstance(value, dict) and value.get("currency"):
        return str(value["currency"]).upper()
    return None


def normalise_product(
    payload: Any,
    *,
    source: str,
    observed_at: datetime,
    as_of_default: datetime | None = None,
    default_currency: str = "USD",
) -> NormalisedProduct:
    if not isinstance(payload, dict):
        raise SourceMalformed("product payload is not an object")
    source_product_id = str(payload.get("id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not source_product_id or not title:
        raise SourceMalformed("product is missing id or title")

    product_id = f"{source}:{source_product_id}"
    observed_at = as_utc(observed_at)
    fallback_as_of = as_of_default or observed_at
    warnings: list[str] = []
    offers: list[Offer] = []
    raw_offers: dict[str, Any] = {}
    selected_options = _selected_options(payload)

    for index, raw in enumerate(payload.get("offers") or []):
        if not isinstance(raw, dict):
            warnings.append(f"{source_product_id}: offer #{index} is not an object; skipped")
            continue
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            warnings.append(f"{source_product_id}: offer #{index} has no valid URL; skipped")
            continue
        parsed_price = _offer_price(raw, default_currency)
        if parsed_price is None:
            warnings.append(f"{source_product_id}: offer #{index} has no usable price; skipped")
            continue
        price, currency = parsed_price
        merchant_domain = normalise_domain(raw.get("domain") or urlsplit(url).netloc)
        if merchant_domain == "unknown":
            warnings.append(f"{source_product_id}: offer #{index} has no merchant domain")
        host = urlsplit(url).netloc.lower()
        is_redirect = any(host == item or host.endswith("." + item) for item in _REDIRECTOR_HOSTS)
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        source_offer_id = f"{source_product_id}:{merchant_domain}:{digest}"
        offer_id = f"{source}:{source_offer_id}"
        shipping = _offer_shipping(raw)
        shipping_currency = _offer_shipping_currency(raw)
        if shipping is not None and shipping_currency and shipping_currency != currency:
            warnings.append(f"{source_product_id}: shipping currency differs; shipping ignored")
            shipping = None
        offers.append(
            offer_from_raw(
                offer_id=offer_id,
                source=source,
                source_offer_id=source_offer_id,
                product_id=product_id,
                offer_product_id=product_id,
                variant_id=raw.get("variant_id") or payload.get("selected_product_id") or None,
                selected_options=selected_options,
                merchant_name=raw.get("merchant_name") or raw.get("merchant") or merchant_domain,
                merchant_domain=merchant_domain,
                price=price,
                currency=currency,
                shipping_cost=shipping,
                availability=parse_availability(raw.get("availability")),
                condition=parse_condition(raw.get("condition")),
                url=url,
                redirect_url=raw.get("merchant_url") if isinstance(raw.get("merchant_url"), str) else None,
                is_redirect=is_redirect,
                observed_at=observed_at,
                as_of=parse_datetime(raw.get("as_of"), fallback_as_of),
            )
        )
        raw_offers[offer_id] = raw

    if not offers:
        warnings.append(f"{source_product_id}: no usable offers")

    product = Product(
        product_id=product_id,
        source=source,
        source_product_id=source_product_id,
        title=title[:1000],
        brand=_first_brand(payload),
        description=str(payload.get("description") or "")[:5000],
        image_url=_image_url(payload),
        category=_category(payload),
        attributes=_attributes(payload),
        offers=tuple(offers),
        observed_at=observed_at,
    )
    return NormalisedProduct(product=product, raw_product=payload, raw_offers=raw_offers, warnings=tuple(warnings))
