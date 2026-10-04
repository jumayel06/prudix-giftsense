"""Gift registry pages and actions, through the App Proxy
(/apps/giftsense/registry… → /api/storefront/registry…). Pro only.

    POST /registry/items              add a product variant (owner, logged in)
    GET  /registry                    the owner's page: details, items, share link, AI ideas
    POST /registry                    update title / occasion / event date
    POST /registry/items/{item_id}    change how many are wanted (0 removes)
    POST /registry/suggestions        AI gift ideas for the registry (metered, cached a day)
    GET  /registry/{share_token}      the guests' page: what's still wanted, add to cart

The owner is Shopify's signed `logged_in_customer_id`, never anything in the
body. Pages are Liquid rendered inside the store's theme, so every piece of
text we print goes through `_t` (HTML-escaped, and `{`/`}` neutralized so
shopper-written titles can't run Liquid).
"""
import html
import json
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import chat
from app.routes.storefront import storefront_shop
from app.services import metering, rate_limit
from app.services import registry as reg
from app.services.gifting.embeddings import OpenAIEmbedder
from core.db.models import Shop
from core.db.session import get_db

router = APIRouter(prefix="/api/storefront")

SUGGESTIONS_PER_REGISTRY_PER_HOUR = 6
ADDS_PER_CUSTOMER_PER_HOUR = 120


def _t(text) -> str:
    return html.escape(str(text or ""), quote=True).replace("{", "&#123;").replace("}", "&#125;")


def _js(value) -> str:
    """A JS string literal that can't close the <script> or start Liquid."""
    return json.dumps(str(value or "")).replace("<", "\\u003c").replace("{", "\\u007b").replace("}", "\\u007d")


def _liquid(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, media_type="application/liquid",
                        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


def _json(payload: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


def _customer(request: Request) -> str | None:
    cid = request.query_params.get("logged_in_customer_id") or ""
    return cid if cid.isdigit() else None


def _require_owner(request: Request, shop: Shop) -> str:
    if not reg.available(shop):
        raise HTTPException(403, "Registries aren't available in this store.")
    cid = _customer(request)
    if not cid:
        raise HTTPException(401, "Please log in to use your registry.")
    return cid


# ── Owner actions ───────────────────────────────────────────────────────────

class AddItem(BaseModel):
    product_id: str = Field(max_length=40, pattern=r"^\d+$")
    variant_id: str = Field(max_length=40, pattern=r"^\d+$")
    variant_title: str = Field(default="", max_length=120)
    quantity: int = Field(default=1, ge=1, le=reg.MAX_WANTED)


@router.post("/registry/items")
async def add_item(body: AddItem, request: Request, shop: Shop = Depends(storefront_shop),
                   db: AsyncSession = Depends(get_db)):
    cid = _require_owner(request, shop)
    if not await rate_limit.hit(f"registry:add:{shop.id}:{cid}", ADDS_PER_CUSTOMER_PER_HOUR, rate_limit.HOUR):
        raise HTTPException(429, "You've added a lot in a short time. Please try again in a little while.")
    try:
        await reg.add_item(db, shop, cid, product_id=body.product_id, variant_id=body.variant_id,
                           variant_title=body.variant_title, quantity=body.quantity)
    except reg.RegistryError as e:
        raise HTTPException(422, str(e))
    registry = await reg.for_owner(db, shop, cid)
    return _json({"count": len(await reg.items_of(db, registry)), "manage_url": "/apps/giftsense/registry"})


class Details(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    occasion: Optional[str] = None
    event_date: Optional[date] = None


@router.post("/registry")
async def update_details(body: Details, request: Request, shop: Shop = Depends(storefront_shop),
                         db: AsyncSession = Depends(get_db)):
    cid = _require_owner(request, shop)
    registry = await reg.get_or_create(db, shop, cid)
    try:
        reg.update_details(registry, body.title, body.occasion, body.event_date)
    except reg.RegistryError as e:
        raise HTTPException(422, str(e))
    await db.commit()
    return _json({"ok": True})


class Wanted(BaseModel):
    wanted_qty: int = Field(ge=0, le=reg.MAX_WANTED)


@router.post("/registry/suggestions")
async def suggestions(request: Request, shop: Shop = Depends(storefront_shop), db: AsyncSession = Depends(get_db)):
    cid = _require_owner(request, shop)
    registry = await reg.for_owner(db, shop, cid)
    if registry is None:
        return _json({"picks": []})
    cached = reg.cached_suggestions(registry)
    if cached is not None:
        return _json({"picks": cached})
    if not await rate_limit.hit(f"registry:ideas:{registry.id}", SUGGESTIONS_PER_REGISTRY_PER_HOUR, rate_limit.HOUR):
        raise HTTPException(429, "Please try again in a little while.")
    items = await reg.items_of(db, registry)
    result = await metering.run_gift_search(db, shop, reg.suggestion_intake(registry, items), OpenAIEmbedder(),
                                            chat_fn=chat, action_type="registry_suggest")
    picks = [{"product_id": p.product.product_id, "title": p.product.title, "url": p.product.url,
              "image_url": p.product.image_url, "price_min": p.product.price_min, "reason": p.reason}
             for p in result.recommendation.picks[:reg.SUGGESTIONS]]
    registry.suggestions = {"day": datetime.now(timezone.utc).date().isoformat(), "picks": picks}
    await db.commit()
    return _json({"picks": picks})


@router.post("/registry/items/{item_id}")
async def update_item(item_id: uuid.UUID, body: Wanted, request: Request, shop: Shop = Depends(storefront_shop),
                      db: AsyncSession = Depends(get_db)):
    cid = _require_owner(request, shop)
    registry = await reg.for_owner(db, shop, cid)
    if registry is None:
        raise HTTPException(404, "No registry yet.")
    try:
        await reg.update_item(db, registry, item_id, body.wanted_qty)
    except reg.RegistryError as e:
        raise HTTPException(404, str(e))
    return _json({"ok": True})


# ── Pages ───────────────────────────────────────────────────────────────────

_CSS = """<style>
.gsr{max-width:960px;margin:32px auto;padding:0 16px;display:grid;gap:20px}
.gsr h1{margin:0}.gsr .muted{opacity:.7;margin:0}
.gsr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}
.gsr-card{border:1px solid rgba(127,127,127,.25);border-radius:12px;padding:12px;display:grid;gap:8px;align-content:start}
.gsr-card img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px}
.gsr-card h3{font-size:1rem;margin:0}.gsr-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.gsr input,.gsr select{padding:8px;border:1px solid rgba(127,127,127,.4);border-radius:8px;font:inherit}
.gsr button{padding:8px 14px;border-radius:999px;border:1px solid currentColor;background:transparent;font:inherit;cursor:pointer}
.gsr button.primary{background:currentColor}.gsr button.primary span{color:#fff;mix-blend-mode:difference}
.gsr button[disabled]{opacity:.5;cursor:default}.gsr .done{color:#1f7a3e}
.gsr-share{display:flex;gap:8px}.gsr-share input{flex:1;min-width:0}
</style>"""


def _money(amount) -> str:
    # Liquid's money filter formats in the store's currency and style.
    return "{{ " + str(int(round(float(amount or 0) * 100))) + " | money }}"


def _occasion_label(code: str) -> str:
    return reg.OCCASIONS.get(code, "Wish list")


def _when(d: date | None) -> str:
    return f"{d:%B} {d.day}, {d.year}" if d else ""


@router.get("/registry")
async def owner_page(request: Request, shop: Shop = Depends(storefront_shop), db: AsyncSession = Depends(get_db)):
    if not reg.available(shop):
        return _liquid(f'{_CSS}<div class="gsr"><h1>Registries aren\'t available</h1></div>', 404)
    cid = _customer(request)
    if not cid:
        return _liquid(f'{_CSS}<div class="gsr"><h1>Your registry</h1><p>Log in to create or manage your gift registry.</p>'
                       '<p><a href="/account/login?return_url=/apps/giftsense/registry">Log in</a></p></div>')
    registry = await reg.for_owner(db, shop, cid)
    if registry is None:
        return _liquid(f'{_CSS}<div class="gsr"><h1>Your registry</h1><p>Your registry is empty. Open any product and '
                       'choose <b>Add to registry</b> to start it.</p></div>')
    items = await reg.items_of(db, registry)
    options = "".join(f'<option value="{k}"{" selected" if k == registry.occasion else ""}>{_t(v)}</option>'
                      for k, v in reg.OCCASIONS.items())
    cards = "".join(
        f'<div class="gsr-card">{f"<img src={_t(i.image_url)!r} alt={_t(i.title)!r} loading=lazy>" if i.image_url else ""}'
        f'<h3>{_t(i.title)}</h3><p class="muted">{_t(i.variant_title)} · {_money(i.price)}</p>'
        f'<div class="gsr-row"><label>Wanted <input type="number" min="0" max="{reg.MAX_WANTED}" value="{i.wanted_qty}" '
        f'data-item="{i.id}" style="width:64px"></label><span class="muted">{i.bought_qty} bought</span></div>'
        f'<button type="button" data-remove="{i.id}">Remove</button></div>'
        for i in items) or '<p class="muted">No items yet. Open any product and choose Add to registry.</p>'
    share = "{{ shop.url }}/apps/giftsense/registry/" + _t(registry.share_token)
    body = f"""{_CSS}<div class="gsr">
<h1>{_t(registry.title)}</h1>
<div class="gsr-row">
  <input id="gsr-title" maxlength="{reg.MAX_TITLE}" value="{_t(registry.title)}" aria-label="Registry name">
  <select id="gsr-occasion" aria-label="Occasion">{options}</select>
  <input id="gsr-date" type="date" value="{registry.event_date.isoformat() if registry.event_date else ''}" aria-label="Event date">
  <button type="button" id="gsr-save">Save</button><span id="gsr-status" class="muted" role="status"></span>
</div>
<div><p class="muted">Share this link with friends and family. They see what's still wanted and buy it in this store.</p>
<div class="gsr-share"><input id="gsr-link" readonly value="{share}"><button type="button" id="gsr-copy">Copy link</button></div></div>
<h2>Items</h2><div class="gsr-grid">{cards}</div>
<h2>Gift ideas</h2><p class="muted">Ideas from this store that go with your registry.</p>
<button type="button" id="gsr-ideas">Show ideas</button><div class="gsr-grid" id="gsr-ideas-list"></div>
</div>
<script>
(function () {{
  var base = '/apps/giftsense/registry';
  function post(path, body) {{
    return fetch(base + path, {{ method: 'POST', credentials: 'same-origin',
      headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }}, body: JSON.stringify(body || {{}}) }});
  }}
  var status = document.getElementById('gsr-status');
  document.getElementById('gsr-save').addEventListener('click', function () {{
    var d = document.getElementById('gsr-date').value;
    post('', {{ title: document.getElementById('gsr-title').value, occasion: document.getElementById('gsr-occasion').value,
      event_date: d || null }}).then(function (r) {{ status.textContent = r.ok ? 'Saved' : 'Could not save'; }});
  }});
  document.getElementById('gsr-copy').addEventListener('click', function () {{
    var input = document.getElementById('gsr-link');
    input.select();
    (navigator.clipboard ? navigator.clipboard.writeText(input.value) : Promise.reject()).catch(function () {{
      document.execCommand('copy');
    }}).then(function () {{ status.textContent = 'Link copied'; }});
  }});
  document.querySelectorAll('[data-item]').forEach(function (input) {{
    input.addEventListener('change', function () {{
      post('/items/' + input.getAttribute('data-item'), {{ wanted_qty: Number(input.value) || 0 }})
        .then(function (r) {{ if (r.ok && Number(input.value) === 0) location.reload(); }});
    }});
  }});
  document.querySelectorAll('[data-remove]').forEach(function (b) {{
    b.addEventListener('click', function () {{
      post('/items/' + b.getAttribute('data-remove'), {{ wanted_qty: 0 }}).then(function () {{ location.reload(); }});
    }});
  }});
  var ideas = document.getElementById('gsr-ideas');
  ideas.addEventListener('click', function () {{
    ideas.disabled = true; ideas.textContent = 'Finding ideas…';
    post('/suggestions').then(function (r) {{ return r.json(); }}).then(function (d) {{
      var list = document.getElementById('gsr-ideas-list');
      ideas.hidden = true;
      (d.picks || []).forEach(function (p) {{
        var card = document.createElement('div'); card.className = 'gsr-card';
        if (p.image_url) {{ var img = document.createElement('img'); img.src = p.image_url; img.alt = ''; card.appendChild(img); }}
        var h = document.createElement('h3'); h.textContent = p.title; card.appendChild(h);
        var why = document.createElement('p'); why.className = 'muted'; why.textContent = p.reason || ''; card.appendChild(why);
        var add = document.createElement('button'); add.type = 'button'; add.textContent = 'Add to registry';
        add.addEventListener('click', function () {{
          add.disabled = true;
          fetch(p.url + '.js').then(function (r) {{ return r.json(); }}).then(function (prod) {{
            var v = (prod.variants || []).filter(function (x) {{ return x.available; }})[0] || prod.variants[0];
            return post('/items', {{ product_id: String(prod.id), variant_id: String(v.id), variant_title: v.title }});
          }}).then(function (r) {{ add.textContent = r.ok ? 'Added ✓' : 'Could not add'; if (r.ok) setTimeout(function () {{ location.reload(); }}, 600); }});
        }});
        card.appendChild(add);
        list.appendChild(card);
      }});
      if (!(d.picks || []).length) list.textContent = 'No ideas right now. Try again tomorrow.';
    }}).catch(function () {{ ideas.disabled = false; ideas.textContent = 'Show ideas'; }});
  }});
}})();
</script>"""
    return _liquid(body)


@router.get("/registry/{share_token}")
async def guest_page(share_token: str, shop: Shop = Depends(storefront_shop), db: AsyncSession = Depends(get_db)):
    registry = await reg.by_token(db, shop, share_token) if reg.available(shop) else None
    if registry is None:
        return _liquid(f'{_CSS}<div class="gsr"><h1>Registry not found</h1>'
                       '<p class="muted">Check the link, or ask the person who shared it.</p></div>', 404)
    registry.views = (registry.views or 0) + 1
    await db.commit()
    items = await reg.items_of(db, registry)
    meta = " · ".join(x for x in (_occasion_label(registry.occasion), _when(registry.event_date)) if x)
    cards = "".join(
        f'<div class="gsr-card">{f"<img src={_t(i.image_url)!r} alt={_t(i.title)!r} loading=lazy>" if i.image_url else ""}'
        f'<h3>{_t(i.title)}</h3><p class="muted">{_t(i.variant_title)}{" · " if i.variant_title else ""}{_money(i.price)}</p>'
        f'<p class="muted">{min(i.bought_qty, i.wanted_qty)} of {i.wanted_qty} bought</p>'
        + (f'<p class="done">All bought ✓</p>' if i.bought_qty >= i.wanted_qty else
           f'<button type="button" data-add="{_t(i.variant_id)}" data-line="{_t(registry.share_token)}:{i.id}">Add to cart</button>')
        + "</div>" for i in items) or '<p class="muted">Nothing on this registry yet.</p>'
    body = f"""{_CSS}<div class="gsr">
<h1>{_t(registry.title)}</h1><p class="muted">{_t(meta)}</p>
<div class="gsr-grid">{cards}</div>
<p class="muted" id="gsr-status" role="status"></p>
</div>
<script>
(function () {{
  var name = {_js(registry.title[:60])};
  document.querySelectorAll('[data-add]').forEach(function (b) {{
    b.addEventListener('click', function () {{
      b.disabled = true;
      fetch('/cart/add.js', {{ method: 'POST', credentials: 'same-origin',
        headers: {{ 'Content-Type': 'application/json', Accept: 'application/json' }},
        body: JSON.stringify({{ items: [{{ id: Number(b.getAttribute('data-add')), quantity: 1,
          properties: {{ _giftsense_registry: b.getAttribute('data-line'), 'Registry': name }} }}] }}) }})
        .then(function (r) {{
          if (!r.ok) throw new Error();
          b.textContent = 'Added ✓';
          document.getElementById('gsr-status').textContent = '';
          var a = document.createElement('a'); a.href = '/cart'; a.textContent = 'View cart and check out';
          document.getElementById('gsr-status').appendChild(a);
        }}).catch(function () {{ b.disabled = false; b.textContent = 'Could not add. Try again'; }});
    }});
  }});
}})();
</script>"""
    return _liquid(body)
