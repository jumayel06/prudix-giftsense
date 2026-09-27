"""Thin GraphQL Admin API POST helper (copied from Prudix Commerce).

GiftSense makes no REST Admin API calls (App Store rule 2.2.4). Callers check
`.status_code` and parse `.json()` themselves, including `userErrors`.
"""
import httpx

from core.config import settings


async def shopify_graphql_post(
    shop_domain: str,
    access_token: str,
    query: str,
    variables: dict | None = None,
    timeout: float = 20,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            f"https://{shop_domain}/admin/api/{settings.shopify_api_version}/graphql.json",
            headers={"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )


def product_gid(numeric_id) -> str:
    return f"gid://shopify/Product/{numeric_id}"


def numeric_id_from_gid(gid: str) -> str:
    """'gid://shopify/Product/123' -> '123'. Also passes plain numeric strings through."""
    return str(gid).rsplit("/", 1)[-1]
