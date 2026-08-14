"""
Shared helper for the "per-size stock is the sole source of truth" model.

The `products.stock` column is kept (queries across the app filter/sort on
it — storefront listing, low/out-of-stock widgets, the atomic optimistic-
lock decrement in create_order — rewriting all of those to read a JSONB sum
instead would be a much larger and riskier change). What changes is WHERE
`stock` comes from: instead of an admin/shopkeeper typing an overall number,
it's now always derived by summing the per-size stock map, so the two can
never drift apart.
"""


def derive_total_stock(size_stock: dict | None, fallback: int = 1) -> int:
    """
    Sums a size_stock map like {"S": 4, "M": 0, "L": 2} into a total.
    Ignores non-numeric/negative junk defensively rather than raising.

    `fallback` is only used when size_stock is empty/missing entirely —
    i.e. a product with no size variants at all (no sizes added), which
    can still exist and needs *some* stock value. Callers pass the
    previous stored stock as the fallback on edits, so a product that
    already had sizes never gets silently reset to 1 just because this
    particular request didn't touch size_stock.
    """
    if not size_stock or not isinstance(size_stock, dict):
        return max(0, fallback)
    total = 0
    for v in size_stock.values():
        if isinstance(v, (int, float)) and v > 0:
            total += int(v)
    return total