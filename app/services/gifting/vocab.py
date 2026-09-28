"""Fixed gift vocabularies (docs/TECHNICAL_PLAN.md §4.2).

The shopper intake offers exactly these values, and the product gift-profile
enrichment must choose from exactly these values, so recipient/occasion/vibe
overlap is exact matching rather than fuzzy. Changing a value means
re-enriching catalogs, so add rather than rename.
"""

RECIPIENTS = [
    "partner", "parent", "grandparent", "sibling", "friend", "coworker",
    "boss", "teacher", "kid", "teen", "new_baby", "host", "anyone",
]

OCCASIONS = [
    "birthday", "anniversary", "holiday", "thank_you", "wedding",
    "new_baby", "housewarming", "graduation", "get_well", "sympathy",
    "retirement", "just_because",
]

VIBES = [
    "cozy", "practical", "funny", "luxurious", "adventurous", "creative",
    "sentimental", "trendy", "classic", "eco_friendly", "sporty", "foodie",
    "relaxing", "techy",
]

AGE_BANDS = ["baby", "kid", "teen", "adult", "any"]

LABELS = {
    # recipients
    "partner": "Partner", "parent": "Parent", "grandparent": "Grandparent",
    "sibling": "Sibling", "friend": "Friend", "coworker": "Coworker",
    "boss": "Boss", "teacher": "Teacher", "kid": "Kid", "teen": "Teen",
    "new_baby": "New baby", "host": "Host", "anyone": "Anyone",
    # occasions
    "birthday": "Birthday", "anniversary": "Anniversary", "holiday": "Holiday",
    "thank_you": "Thank you", "wedding": "Wedding", "housewarming": "Housewarming",
    "graduation": "Graduation", "get_well": "Get well", "sympathy": "Sympathy",
    "retirement": "Retirement", "just_because": "Just because",
    # vibes
    "cozy": "Cozy", "practical": "Practical", "funny": "Funny",
    "luxurious": "Luxurious", "adventurous": "Adventurous", "creative": "Creative",
    "sentimental": "Sentimental", "trendy": "Trendy", "classic": "Classic",
    "eco_friendly": "Eco-friendly", "sporty": "Sporty", "foodie": "Foodie",
    "relaxing": "Relaxing", "techy": "Techy",
    # age bands
    "baby": "Baby", "adult": "Adult", "any": "Any age",
}

# Budget bands in the shop's currency. (min, max); max None = no upper bound.
BUDGET_BANDS = {
    "under_25": (0, 25),
    "25_50": (25, 50),
    "50_100": (50, 100),
    "100_200": (100, 200),
    "200_plus": (200, None),
}


def budget_range(band: str) -> tuple[float, float | None]:
    try:
        return BUDGET_BANDS[band]
    except KeyError:
        raise ValueError(f"unknown budget band: {band!r}") from None


def clean_list(values, allowed: list[str]) -> list[str]:
    """Normalize to lowercase, keep only allowed values, drop duplicates, keep order."""
    out: list[str] = []
    for v in values or []:
        v = str(v).strip().lower()
        if v in allowed and v not in out:
            out.append(v)
    return out
