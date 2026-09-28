"""Catalog product shape used by the gift-matching engine.

Built from Shopify product data by the catalog sync (week 3); built from JSON
fixtures in tests and the offline eval. Prices are in the shop's currency.
"""
from dataclasses import dataclass, field


@dataclass
class CatalogProduct:
    product_id: str
    title: str
    description: str = ""
    product_type: str = ""
    vendor: str = ""
    tags: list[str] = field(default_factory=list)
    price_min: float = 0.0
    price_max: float = 0.0
    available: bool = True
    image_url: str | None = None
    url: str | None = None
    excluded: bool = False
