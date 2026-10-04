"""Shared branded primitives for GiftSense's merchant emails (weekly email,
theme alert). Copied from Prudix Commerce so both apps' emails look the same;
GiftSense changes: the header chip label is a parameter, and the bar chart
takes any number of bars (7 days, not only 4 weeks).

Design principles:

* Palette pulled from the Prudix marketing site (prudix-website global.css)
  so the emails feel like the web brand: slate-900 navy `#0f172a` for text +
  dark surfaces, brand yellow `#eab308` for accents + CTAs, slate scale for
  borders and muted copy. Inter typeface with a system-font fallback stack.

* Email-client bulletproof layout: nested `<table>` grids instead of flexbox
  or grid, so Outlook / Gmail mobile / Apple Mail all render identically.

* Merchant deep-links target the embedded app inside Shopify admin
  (`https://admin.shopify.com/store/{store}/apps/{handle}/{path}`), NOT our
  marketing domain — merchants live inside Shopify admin and the CTA needs
  to drop them straight into the relevant Prudix page there.

* Brand promotion: dark navy header with the P.X logo mark + Prudix wordmark;
  footer includes a "Powered by Prudix" link back to prudix.app. Both surfaces
  survive image-blocking clients (Outlook) because the mark is inline-CSS,
  not an <img>.
"""
from __future__ import annotations

import html as _html
from typing import Iterable

from core.config import settings as core_settings


# ── Brand palette (must stay in sync with prudix-website global.css) ─────

NAVY_900        = "#0f172a"   # primary text, dark surfaces
NAVY_800        = "#1e293b"   # elevated dark surfaces
NAVY_700        = "#334155"
SLATE_600       = "#475569"   # muted body copy
SLATE_500       = "#64748b"
SLATE_400       = "#94a3b8"   # faint labels
SLATE_300       = "#cbd5e1"
SLATE_200       = "#e2e8f0"   # borders
SLATE_100       = "#f1f5f9"   # alt band
SLATE_50        = "#f8fafc"   # page bg
WHITE           = "#ffffff"

BRAND_YELLOW    = "#eab308"   # yellow-500 — CTAs, badges, accents
BRAND_YELLOW_DK = "#a16207"   # yellow-700 — text on white
BRAND_YELLOW_XD = "#854d0e"   # yellow-800 — hover / active

ACCENT_INDIGO   = "#6366f1"   # secondary series (retention, inventory sub-metric)
ACCENT_EMERALD  = "#10b981"   # positive delta / cleared / recovered
ACCENT_ROSE     = "#ef4444"   # at-risk / negative / stockout

FONT_STACK = ("Inter, -apple-system, BlinkMacSystemFont, "
              "'Segoe UI', Roboto, sans-serif")

# Wordmark stack — Poppins is what the app sidebar + footer use.
# Emails that can't load Google Fonts (Gmail, Outlook) fall back to a
# bold system font; the letter-spacing / weight still communicates the
# same visual weight.
WORDMARK_FONT_STACK = ("'Poppins', Inter, -apple-system, "
                       "BlinkMacSystemFont, 'Segoe UI', sans-serif")


# ── URL helpers ─────────────────────────────────────────────────────────


def _store_handle(shop_domain: str) -> str:
    """`teststatsdemo.myshopify.com` -> `teststatsdemo`."""
    return shop_domain.replace(".myshopify.com", "").strip()


def build_admin_deep_link(
    shop_domain: str, path: str = "", app_handle: str | None = None
) -> str:
    """Build a Shopify Admin embedded-app deep link.

    `path` can start with a leading slash or not. Query strings and
    fragments are preserved. If `path` looks like it already includes
    a full URL (e.g. legacy digest data has `https://commerce.prudix.app/...`),
    strip everything up to the pathname so we don't double up the domain.
    """
    # Shopify admin also opens an embedded app by its client id, so links
    # still work when SHOPIFY_APP_HANDLE isn't set (it was empty in dev and
    # every email link came out as /apps//page).
    handle = app_handle or core_settings.shopify_app_handle or core_settings.shopify_api_key
    store = _store_handle(shop_domain)
    # Normalize path — accept full URLs, relative paths, or empty.
    if path.startswith("http://") or path.startswith("https://"):
        # Extract path + query + fragment from the URL
        from urllib.parse import urlparse
        parsed = urlparse(path)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "") \
               + (f"#{parsed.fragment}" if parsed.fragment else "")
    path = path.lstrip("/")
    base = f"https://admin.shopify.com/store/{store}/apps/{handle}"
    return f"{base}/{path}" if path else base


def build_unsubscribe_url(shop_domain: str) -> str:
    return build_admin_deep_link(shop_domain, "settings")


# ── Formatting ──────────────────────────────────────────────────────────


def format_currency_cents(cents: int | None) -> str:
    if not cents:
        return "$0"
    dollars = cents / 100
    if dollars >= 1_000_000:
        return f"${dollars/1_000_000:.1f}M"
    if dollars >= 1000:
        return f"${dollars/1000:.1f}k"
    return f"${dollars:,.2f}"


# ── Shell (header + footer wrap the body content) ───────────────────────


def render_shell_top(*, product_name: str, subject_preview: str,
                     hero_title: str, hero_subtitle: str, chip: str = "Weekly digest") -> str:
    """Dark-navy branded header + shell open.

    `subject_preview` becomes the Gmail/Apple Mail preheader (the little
    preview text next to the subject line in the inbox).
    """
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{_html.escape(hero_title)}</title>
<!-- Poppins for the PrudiX wordmark (matches the app sidebar + footer).
     Clients that ignore <link> in email (Gmail, some Outlook) fall back
     to a bold system font — letter-spacing + weight preserved. -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@700;800&display=swap" rel="stylesheet">
<style>
  /* Mobile: stack the header/footer right-column (badge, link) onto its
     own row so it doesn't collide with the wordmark subtitle at narrow
     widths. Gmail iOS + Apple Mail + Outlook Web respect this. */
  @media only screen and (max-width: 480px) {{
    .m-stack {{ display: block !important; width: 100% !important; }}
    .m-right-stack {{
      text-align: left !important;
      padding-top: 14px !important;
      padding-left: 46px !important;
    }}
  }}
</style>
</head>
<body style="margin: 0; padding: 0; background: {SLATE_100};
             font-family: {FONT_STACK}; color: {NAVY_900};">
<!-- Preheader: shows in inbox preview, hidden in email body -->
<div style="display: none; max-height: 0; overflow: hidden;
            font-size: 1px; line-height: 1px; color: {SLATE_100};">
{_html.escape(subject_preview)}
</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background: {SLATE_100}; padding: 24px 0;">
  <tr><td align="center">
    <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0"
           style="width: 640px; max-width: 100%; background: {WHITE};
                  border-radius: 16px; overflow: hidden;
                  box-shadow: 0 1px 3px rgba(15,23,42,0.05),
                              0 4px 24px -8px rgba(15,23,42,0.12);">

      <!-- Header: dark navy with PrudiX wordmark + product tag -->
      <tr><td style="background: linear-gradient(135deg,
              {NAVY_900} 0%, {NAVY_800} 60%, {NAVY_700} 100%);
              padding: 22px 32px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <!-- Logo mark — hosted PNG (Gmail strips inline SVG entirely).
                 Retina source at 128px, displayed 32×32. Alt text shows if
                 the client blocks external images (Outlook default). -->
            <td style="vertical-align: middle; width: 40px;">
              <img src="https://prudix.app/logo-on-dark-128.png"
                   width="32" height="32" alt="PrudiX"
                   style="display: block; border: 0;">
            </td>
            <!-- PrudiX wordmark — split spans: "Prudi" slate-400 (#94a3b8)
                 + "X" brand-yellow (#eab308). Matches the silver→yellow feel of
                 the app footer without relying on background-clip:text (which
                 Gmail strips entirely). Works in all email clients. -->
            <td style="vertical-align: middle; padding-left: 6px;">
              <div style="line-height: 1;">
                <span style="font-family: {WORDMARK_FONT_STACK}; font-weight: 800;
                             font-size: 22px; letter-spacing: -0.04em;"><span style="color: #94a3b8;">Prudi</span><span style="color: {BRAND_YELLOW};">X<sup style="font-size: 11px; font-weight: 600; margin-left: 2px;">&trade;</sup></span></span>
              </div>
              <div style="color: {SLATE_400}; font-size: 11px;
                          text-transform: uppercase; letter-spacing: 0.08em;
                          margin-top: 8px; font-family: {FONT_STACK};">
                {_html.escape(product_name)}
              </div>
            </td>
            <!-- Right-aligned "weekly digest" chip -->
            <td align="right" class="m-stack m-right-stack" style="vertical-align: middle;">
              <span style="display: inline-block; padding: 4px 10px;
                           border-radius: 999px; font-size: 11px;
                           font-weight: 600; letter-spacing: 0.04em;
                           text-transform: uppercase;
                           background: rgba(234, 179, 8, 0.16);
                           color: {BRAND_YELLOW};">{_html.escape(chip)}</span>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- Hero: big framed narrative -->
      <tr><td style="padding: 28px 32px 20px;">
        <h1 style="margin: 0 0 8px; font-size: 22px; font-weight: 700;
                   color: {NAVY_900}; letter-spacing: -0.015em;
                   line-height: 1.25;">{_html.escape(hero_title)}</h1>
        <p style="margin: 0; font-size: 14px; line-height: 1.6;
                  color: {SLATE_600};">{_html.escape(hero_subtitle)}</p>
      </td></tr>
"""


def render_shell_bottom(*, shop_domain: str, product_name: str,
                        primary_cta_label: str, primary_cta_path: str) -> str:
    """Primary CTA card + brand-promo footer + unsubscribe."""
    cta_url = build_admin_deep_link(shop_domain, primary_cta_path)
    unsub_url = build_unsubscribe_url(shop_domain)
    return f"""
      <!-- Primary CTA — deep-link back into the app in Shopify admin -->
      <tr><td style="padding: 8px 32px 28px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr><td align="center"
                  style="background: {NAVY_900}; border-radius: 10px;
                         padding: 14px 24px;">
            <a href="{cta_url}"
               style="color: {WHITE}; font-weight: 600; font-size: 14px;
                      text-decoration: none; display: inline-block;
                      letter-spacing: 0.01em;">{_html.escape(primary_cta_label)}
              <span style="color: {BRAND_YELLOW}; margin-left: 6px;">&rarr;</span>
            </a>
          </td></tr>
        </table>
      </td></tr>

      <!-- Brand footer — dark, promotes prudix.app -->
      <tr><td style="background: {NAVY_900}; padding: 22px 32px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>
            <!-- Same hosted PNG the header uses. -->
            <td style="vertical-align: middle; width: 34px;">
              <img src="https://prudix.app/logo-on-dark-128.png"
                   width="26" height="26" alt="PrudiX"
                   style="display: block; border: 0;">
            </td>
            <td style="vertical-align: middle; padding-left: 4px;">
              <div style="line-height: 1;">
                <span style="font-family: {WORDMARK_FONT_STACK}; font-weight: 800;
                             font-size: 20px; letter-spacing: -0.04em;"><span style="color: #94a3b8;">Prudi</span><span style="color: {BRAND_YELLOW};">X<sup style="font-size: 10px; font-weight: 600; margin-left: 2px;">&trade;</sup></span></span>              </div>
              <div style="color: {SLATE_400}; font-family: {FONT_STACK};
                          font-size: 12px; margin-top: 6px;">
                Focused Shopify apps that grow your store.
              </div>
            </td>
            <td align="right" class="m-stack m-right-stack" style="vertical-align: middle;">
              <a href="https://prudix.app"
                 style="color: {BRAND_YELLOW}; font-size: 12px; font-weight: 600;
                        text-decoration: none;">prudix.app &rarr;</a>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- CAN-SPAM unsubscribe -->
      <tr><td style="padding: 16px 32px 20px; background: {SLATE_50};
                     font-size: 12px; color: {SLATE_500}; text-align: center;
                     line-height: 1.5;">
        <a href="{unsub_url}" style="color: {BRAND_YELLOW_DK};
           text-decoration: underline;">Manage notifications</a>
        &middot; <a href="{unsub_url}" style="color: {SLATE_500};
           text-decoration: underline;">Unsubscribe</a>
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""


# ── Metric grid (2×2 bulletproof table) ─────────────────────────────────


def render_metric_grid_2col(items: list[dict]) -> str:
    """Render up to 4 metric tiles in a 2×2 table grid.

    Each item: `{label, value, sub?, tone?}` where tone is one of
    'default', 'positive', 'warning', 'brand'. Table layout (not flex)
    is bulletproof across Outlook + Gmail mobile — the retention email
    was wrapping into 4 separate lines with the old flex layout.
    """
    if not items:
        return ""
    # Pad to even count so the 2-col grid is symmetric
    if len(items) % 2 == 1:
        items = items + [{"label": "", "value": ""}]

    rows_html = ""
    for i in range(0, len(items), 2):
        left = _tile(items[i])
        right = _tile(items[i + 1])
        rows_html += f"""
        <tr>
          <td width="50%" style="padding: 0 6px 12px 0;">{left}</td>
          <td width="50%" style="padding: 0 0 12px 6px;">{right}</td>
        </tr>"""
    return f"""
      <tr><td style="padding: 4px 32px 8px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               border="0" style="border-collapse: separate;">
          {rows_html}
        </table>
      </td></tr>"""


def _tile(item: dict) -> str:
    label = item.get("label") or ""
    value = item.get("value") or ""
    sub = item.get("sub")
    tone = item.get("tone", "default")
    if not label and not value:
        # Empty pad cell
        return '<div style="height: 1px;"></div>'

    tone_map = {
        "default":  (SLATE_50, SLATE_200, NAVY_900),
        "positive": ("#ecfdf5", "#a7f3d0", "#065f46"),
        "warning":  ("#fff7ed", "#fed7aa", "#9a3412"),
        "brand":    ("#fefce8", "#fde68a", BRAND_YELLOW_XD),
    }
    bg, border, value_color = tone_map.get(tone, tone_map["default"])

    # Always render the sub row — with &nbsp; when absent — so tiles
    # with and without `sub` share the same rendered height in the 2×2
    # grid. min-height alone doesn't help because tiles WITH sub still
    # grow taller than the floor.
    sub_html = (f'<div style="font-size: 11px; color: {SLATE_500}; margin-top: 4px;">'
                f'{_html.escape(sub) if sub else "&nbsp;"}</div>')
    # min-height keeps all tiles the same visual height even when only
    # some have a `sub` line — otherwise the 2×2 grid looks ragged.
    return f"""
<div style="background: {bg}; border: 1px solid {border}; border-radius: 10px;
            padding: 14px 16px; min-height: 74px;">
  <div style="font-size: 11px; font-weight: 600; color: {SLATE_500};
              text-transform: uppercase; letter-spacing: 0.06em;">
    {_html.escape(label)}
  </div>
  <div style="font-size: 22px; font-weight: 700; color: {value_color};
              margin-top: 6px; letter-spacing: -0.015em;">{_html.escape(str(value))}</div>
  {sub_html}
</div>"""


# ── Sparkline w/ empty-state ────────────────────────────────────────────


def render_sparkline(*, weeks: list[dict], value_key: str, title: str,
                     bar_color: str = None,
                     empty_message: str = "Trend chart will populate after "
                                          "the first week of measurable activity.",
                     label_formatter=format_currency_cents) -> str:
    """4-week bar chart. Skips render entirely if `weeks` is empty; shows
    a helpful empty-state message if all values are zero."""
    if not weeks:
        return ""
    bar_color = bar_color or BRAND_YELLOW
    values = [(w.get(value_key) or 0) for w in weeks]
    max_v = max(values) or 1
    all_zero = not any(v > 0 for v in values)

    if all_zero:
        return f"""
      <tr><td style="padding: 4px 32px 20px;">
        <div style="background: {SLATE_50}; border: 1px dashed {SLATE_300};
                    border-radius: 10px; padding: 20px; text-align: center;">
          <div style="font-size: 13px; color: {SLATE_500}; font-weight: 600;
                      margin-bottom: 4px;">{_html.escape(title)}</div>
          <div style="font-size: 12px; color: {SLATE_500}; line-height: 1.5;">
            {_html.escape(empty_message)}
          </div>
        </div>
      </td></tr>"""

    bars_html = ""
    for w, v in zip(weeks, values):
        height_pct = max(6, int(100 * v / max_v))
        label = label_formatter(v) if callable(label_formatter) else str(v)
        week_label = w.get("label") or (w.get("week_start") or "")[:10]
        bar_bg = bar_color if v > 0 else SLATE_200
        bars_html += f"""
        <td width="{100 // max(1, len(weeks))}%" style="padding: 0 4px; vertical-align: bottom;">
          <div style="text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: {NAVY_900};
                        margin-bottom: 6px;">{_html.escape(label)}</div>
            <div style="background: {SLATE_100}; height: 80px;
                        border-radius: 6px 6px 0 0; position: relative;">
              <div style="background: {bar_bg};
                          height: {height_pct}%; width: 100%; position: absolute;
                          bottom: 0; border-radius: 6px 6px 0 0;"></div>
            </div>
            <div style="font-size: 10px; color: {SLATE_400}; margin-top: 6px;
                        font-family: ui-monospace, SF Mono, monospace;">
              {_html.escape(week_label)}
            </div>
          </div>
        </td>"""
    return f"""
      <tr><td style="padding: 4px 32px 20px;">
        <div style="font-size: 13px; font-weight: 600; color: {NAVY_900};
                    margin-bottom: 12px;">{_html.escape(title)}</div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
          <tr>{bars_html}</tr>
        </table>
      </td></tr>"""


# ── Section header (used between metric grid and sub-lists) ─────────────


def render_section_title(text: str) -> str:
    return f"""
      <tr><td style="padding: 8px 32px 4px;">
        <div style="font-size: 13px; font-weight: 700; color: {NAVY_900};
                    letter-spacing: -0.005em;
                    text-transform: uppercase; letter-spacing: 0.06em;
                    color: {SLATE_500};">{_html.escape(text)}</div>
      </td></tr>"""


# ── Recommendation card row (used for top opportunities/leaks/etc.) ─────


def render_recommendation_card(*, shop_domain: str, title: str,
                               summary: str, action_path: str,
                               badge: str | None = None,
                               dollar_impact_cents: int | None = None) -> str:
    """A single 'here's what to do next' card with product title, one-line
    summary, dollar impact chip, and a right-arrow linking into the app."""
    url = build_admin_deep_link(shop_domain, action_path)
    badge_html = ""
    if badge:
        badge_html = f"""
<span style="display: inline-block; padding: 2px 8px; border-radius: 999px;
             font-size: 10px; font-weight: 700; letter-spacing: 0.05em;
             text-transform: uppercase; background: {SLATE_100};
             color: {SLATE_600}; margin-left: 8px; vertical-align: middle;">
  {_html.escape(badge)}
</span>"""
    dollar_html = ""
    if dollar_impact_cents:
        dollar_html = f"""
<div style="font-size: 15px; font-weight: 700; color: {BRAND_YELLOW_DK};
            white-space: nowrap; margin-left: 12px;">
  {format_currency_cents(dollar_impact_cents)}
</div>"""

    return f"""
<a href="{url}" style="display: block; padding: 14px 16px;
    background: {WHITE}; border: 1px solid {SLATE_200};
    border-radius: 10px; margin-bottom: 8px; text-decoration: none;
    color: {NAVY_900};">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td style="vertical-align: middle;">
        <div style="font-size: 14px; font-weight: 600; color: {NAVY_900};
                    letter-spacing: -0.005em;">{_html.escape(title)}{badge_html}</div>
        <div style="font-size: 13px; color: {SLATE_600}; line-height: 1.5;
                    margin-top: 4px;">{_html.escape(summary)}</div>
      </td>
      <td align="right" style="vertical-align: middle; white-space: nowrap;">
        {dollar_html}
      </td>
    </tr>
  </table>
</a>"""


def wrap_recommendations(cards_html: str, section_title: str) -> str:
    if not cards_html.strip():
        return ""
    return f"""
      <tr><td style="padding: 4px 32px 20px;">
        <div style="font-size: 13px; font-weight: 700; color: {SLATE_500};
                    text-transform: uppercase; letter-spacing: 0.06em;
                    margin-bottom: 10px;">{_html.escape(section_title)}</div>
        {cards_html}
      </td></tr>"""


__all__ = [
    "NAVY_900", "NAVY_800", "SLATE_600", "SLATE_500", "SLATE_400",
    "SLATE_200", "SLATE_100", "SLATE_50", "WHITE",
    "BRAND_YELLOW", "BRAND_YELLOW_DK", "ACCENT_INDIGO",
    "ACCENT_EMERALD", "ACCENT_ROSE", "FONT_STACK",
    "build_admin_deep_link", "build_unsubscribe_url",
    "format_currency_cents",
    "render_shell_top", "render_shell_bottom",
    "render_metric_grid_2col", "render_sparkline",
    "render_section_title", "render_recommendation_card",
    "wrap_recommendations",
]
