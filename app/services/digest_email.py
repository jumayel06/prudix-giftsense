"""The weekly GiftSense email (Growth+): last 7 days vs the 7 before.

Built on the shared Prudix email shell (app/services/email_style.py, same as
Prudix Commerce): navy header, a headline and one-paragraph summary, a 2×2
metric grid with ▲/▼ against last week, gift orders per day, and up to two
"what to do next" cards (`tips`, chosen by the worker from the shop's setup).
Only the merchant's own numbers; nothing about shoppers.
"""
from datetime import date

from app.services import email_style as es

PRODUCT = "GiftSense"


def _money(v: float, currency: str) -> str:
    symbol = {"USD": "$", "CAD": "CA$", "AUD": "A$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{v:,.0f}" if symbol else f"{v:,.0f} {currency}".strip()


def _pct(v) -> str:
    return "—" if v is None else f"{round(v * 100)}%"


def _delta(cur, prev, kind: str) -> str:
    if cur is None or prev is None:
        return ""
    if kind == "pct":
        diff = round((cur - prev) * 100)
        return "" if diff == 0 else f"{'▲' if diff > 0 else '▼'} {abs(diff)} pts"
    if not prev:
        return "new" if cur else ""
    pct = round((cur - prev) / prev * 100)
    return "" if pct == 0 else f"{'▲' if pct > 0 else '▼'} {abs(pct)}%"


def app_url(shop_domain: str, page: str = "analytics") -> str:
    """Deep link into the embedded app in Shopify admin (handle, else client id)."""
    return es.build_admin_deep_link(shop_domain, page)


def subject_for(week: dict) -> str:
    currency = week.get("currency") or "USD"
    orders = week["gift_orders"]
    return (f"Your GiftSense week: {orders} gift order{'' if orders == 1 else 's'}, "
            f"{_money(week['attributed_revenue'], currency)} from the gift finder")


def _summary(week: dict, before: dict) -> str:
    orders, prev = week["gift_orders"], before.get("gift_orders") or 0
    parts = []
    if orders:
        trend = ("up from" if orders > prev else "down from" if orders < prev else "the same as") + f" {prev} the week before"
        parts.append(f"You had {orders} gift order{'' if orders == 1 else 's'} this week, {trend}.")
    else:
        parts.append("No gift orders this week yet.")
    if week.get("sessions"):
        parts.append(f"{week['sessions']:,} shopper{'' if week['sessions'] == 1 else 's'} used the gift finder.")
    if week.get("all_orders"):
        parts.append(f"{round(100 * orders / week['all_orders'])}% of your {week['all_orders']} orders were gifts.")
    if week.get("top_occasions"):
        parts.append(f"Most searched occasion: {week['top_occasions'][0]['label']}.")
    return " ".join(parts)


def render(shop_name: str, shop_domain: str, week: dict, before: dict,
           tips: list[tuple[str, str, str]] | None = None) -> tuple[str, str, str]:
    """(subject, html, text). `tips`: [(title, summary, app page path)]."""
    currency = week.get("currency") or before.get("currency") or "USD"
    subject = subject_for(week)
    summary = _summary(week, before)

    def tile(label, key, kind, tone="default"):
        value = (_money(week.get(key) or 0, currency) if kind == "money" else _pct(week.get(key)) if kind == "pct"
                 else f"{week.get(key) or 0:,}")
        delta = _delta(week.get(key), before.get(key), kind)
        return {"label": label, "value": value, "sub": f"{delta} vs last week" if delta else "vs last week: no change",
                "tone": tone}

    tiles = [tile("Gift orders", "gift_orders", "count", "brand"),
             tile("Revenue from the gift finder", "attributed_revenue", "money", "positive"),
             tile("Gift finder sessions", "sessions", "count"),
             tile("Search to order", "conversion_rate", "pct")]

    html = es.render_shell_top(product_name=PRODUCT, subject_preview=summary, hero_title=subject.split(": ", 1)[-1].capitalize(),
                               hero_subtitle=summary, chip="Weekly report")
    html += es.render_metric_grid_2col(tiles)
    daily = week.get("daily") or []
    if daily:
        html += es.render_sparkline(
            weeks=[{"label": date.fromisoformat(d["date"]).strftime("%a"), "gift_orders": d["gift_orders"]}
                   for d in daily[-7:]],
            value_key="gift_orders", title="Gift orders per day",
            empty_message="Gift orders will show here day by day as they come in.",
            label_formatter=lambda v: str(int(v)))
    cards = "".join(es.render_recommendation_card(shop_domain=shop_domain, title=t, summary=s, action_path=path)
                    for t, s, path in (tips or [])[:2])
    html += es.wrap_recommendations(cards, "What to do next")
    html += es.render_section_title("Gift notes")
    html += (f'<tr><td style="padding: 4px 32px 20px; font-size: 14px; color: {es.SLATE_600}; line-height: 1.6;">'
             f"{_pct(week.get('note_acceptance_rate'))} of gift notes were written with GiftSense AI "
             f"({_delta(week.get('note_acceptance_rate'), before.get('note_acceptance_rate'), 'pct') or 'no change'} vs last week)."
             "</td></tr>")
    html += es.render_shell_bottom(shop_domain=shop_domain, product_name=PRODUCT,
                                   primary_cta_label="See full analytics", primary_cta_path="analytics")

    text = "\n".join([
        f"Your gifting week at {shop_name} (last 7 days vs the 7 before)", "", summary, "",
        *(f"{t['label']}: {t['value']} ({t['sub']})" for t in tiles),
        f"Gift notes written with AI: {_pct(week.get('note_acceptance_rate'))}", "",
        *(f"Next: {t} — {s}" for t, s, _ in (tips or [])[:2]),
        f"See full analytics: {app_url(shop_domain)}", "",
        f"Turn this email off in GiftSense → Settings: {app_url(shop_domain, 'settings')}",
    ])
    return subject, html, text


def alert(shop_domain: str, *, title: str, body: list[str], cta_label: str, cta_path: str,
          chip: str = "Heads up") -> tuple[str, str]:
    """A one-message email in the same shell (theme alerts). Returns (html, text)."""
    html = es.render_shell_top(product_name=PRODUCT, subject_preview=body[0] if body else title,
                               hero_title=title, hero_subtitle=body[0] if body else "", chip=chip)
    for line in body[1:]:
        html += (f'<tr><td style="padding: 0 32px 16px; font-size: 14px; color: {es.SLATE_600}; line-height: 1.6;">'
                 f"{es._html.escape(line)}</td></tr>")
    html += es.render_shell_bottom(shop_domain=shop_domain, product_name=PRODUCT,
                                   primary_cta_label=cta_label, primary_cta_path=cta_path)
    return html, "\n\n".join([*body, app_url(shop_domain, cta_path)])
