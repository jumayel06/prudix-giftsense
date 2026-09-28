"""Offline gift-finder evaluation runner (docs/TECHNICAL_PLAN.md §10.3).

Steps (each cached, so reruns only pay for what changed):
  1. catalogs   — generated once → evals/data/catalogs/<store>.json   (committed)
  2. profiles   — enrichment per product → evals/data/profiles/<store>.json (committed)
  3. vectors    — embeddings → evals/results/cache/ (gitignored, ~free to redo)
  4. searches   — every persona × model through the real pipeline
  5. labels     — Sonnet 5 judge marks acceptable products for each persona
                  over a pool (all models' picks + top retrieval candidates),
                  and flags reasons not backed by the product facts.
                  Cached per (persona, product) → evals/data/labels/<store>.json
  6. report     — evals/results/report.md + report.json + review.html

Everything spends through one Ledger with a hard cap. Internal tooling only;
never imported by the app.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import asdict
from pathlib import Path

from app.llm import chat
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.embeddings import Embedder
from app.services.gifting.enrichment import CATALOG_MODEL, PROMPT_VERSION, enrich_product
from app.services.gifting.pipeline import recommend
from app.services.gifting.profile import GiftProfile, embedding_text, strip_html
from app.services.gifting.retrieval import IndexedProduct, Intake, in_budget, query_text, retrieve
from evals.catalogs import STORES, generate_catalog
from evals.ledger import Ledger
from evals.metrics import SearchOutcome, score_search, summarize

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"

JUDGE_MODEL = "claude-sonnet-5"
EST_ENRICH_USD = 0.004
EST_SEARCH_USD = {"gpt-4o-mini": 0.001, "claude-haiku-4-5": 0.008, "gpt-4.1": 0.012, "claude-sonnet-5": 0.02}
EST_JUDGE_USD = 0.03
POOL_TOP_K = 15
REVIEW_SAMPLE_RATE = 0.2


def _read(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _product(d: dict) -> CatalogProduct:
    return CatalogProduct(**{k: d[k] for k in (
        "product_id", "title", "description", "product_type", "vendor", "tags",
        "price_min", "price_max", "available") if k in d})


class EvalRun:
    def __init__(self, stores: list[str], models: list[str], ledger: Ledger, embedder: Embedder,
                 chat_fn=chat, data_dir: Path = DATA, results_dir: Path = RESULTS,
                 concurrency: int = 6, log=print):
        self.stores = stores
        self.models = models
        self.ledger = ledger
        self.embedder = embedder
        self.chat_fn = chat_fn
        self.data = data_dir
        self.results = results_dir
        self.sem = asyncio.Semaphore(concurrency)
        self.log = log

    # 1. catalogs ────────────────────────────────────────────────────────────
    async def catalog(self, store: str) -> list[dict]:
        path = self.data / "catalogs" / f"{store}.json"
        products = _read(path, None)
        if products is None:
            self.log(f"[{store}] generating {STORES[store]['count']} products…")
            products = await generate_catalog(store, STORES[store], chat_fn=self.chat_fn, ledger=self.ledger)
            _write(path, products)
        return products

    # 2. profiles ────────────────────────────────────────────────────────────
    async def profiles(self, store: str, products: list[dict]) -> dict[str, dict]:
        path = self.data / "profiles" / f"{store}.json"
        cache = _read(path, {})
        todo = [p for p in products if cache.get(p["product_id"], {}).get("_version") != PROMPT_VERSION]
        if todo:
            self.log(f"[{store}] writing gift profiles for {len(todo)} products…")

        async def one(p):
            async with self.sem:
                self.ledger.check(EST_ENRICH_USD)
                r = await enrich_product(_product(p), chat_fn=self.chat_fn)
                self.ledger.charge("profiles", CATALOG_MODEL, r.input_tokens, r.output_tokens)
                cache[p["product_id"]] = {**asdict(r.profile), "_version": PROMPT_VERSION, "_fallback": r.used_fallback}

        try:
            await asyncio.gather(*(one(p) for p in todo))
        finally:
            _write(path, cache)  # keep whatever finished, even if the budget stopped us
        return cache

    # 3. vectors ─────────────────────────────────────────────────────────────
    async def index(self, store: str, products: list[dict], profiles: dict) -> list[IndexedProduct]:
        path = self.results / "cache" / f"{store}.vectors.json"
        cache = _read(path, {})
        items = []
        texts, keys = [], []
        for p in products:
            prof = GiftProfile(**{k: v for k, v in profiles[p["product_id"]].items() if not k.startswith("_")})
            prod = _product(p)
            text = embedding_text(prod, prof)
            items.append((prod, prof, text))
            if cache.get(p["product_id"], {}).get("text") != text:
                texts.append(text)
                keys.append(p["product_id"])
        for i in range(0, len(texts), 256):
            vecs = await self.embedder.embed(texts[i:i + 256])
            self.ledger.charge_usd("embeddings", sum(len(t) for t in texts[i:i + 256]) / 4 * 0.02 / 1e6)
            for k, t, v in zip(keys[i:i + 256], texts[i:i + 256], vecs):
                cache[k] = {"text": t, "vector": v}
        if texts:
            _write(path, cache)
        return [IndexedProduct(prod, prof, cache[prod.product_id]["vector"]) for prod, prof, _ in items]

    # 4. searches ────────────────────────────────────────────────────────────
    async def search(self, store: str, persona: dict, model: str, items: list[IndexedProduct]) -> dict:
        intake = Intake(**{k: persona[k] for k in ("recipient", "occasion", "budget_band", "vibes")},
                        age_band=persona.get("age_band"), free_text=persona.get("free_text", ""))
        async with self.sem:
            self.ledger.check(EST_SEARCH_USD.get(model, 0.02))
            started = time.perf_counter()
            rec = await recommend(intake, items, embedder=self.embedder, model=model, chat_fn=self.chat_fn)
            latency_ms = int((time.perf_counter() - started) * 1000)
        self.ledger.charge(f"search:{model}", model, rec.input_tokens, rec.output_tokens)
        return {"persona": persona["id"], "model": model, "latency_ms": latency_ms, "mode": rec.mode,
                "used_fallback": rec.used_fallback, "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
                "picks": [{"product_id": p.product.product_id, "reason": p.reason, "source": p.source}
                          for p in rec.picks]}

    # 5. labels ──────────────────────────────────────────────────────────────
    async def judge(self, store: str, persona: dict, pool: list[IndexedProduct], runs: list[dict],
                    labels: dict) -> list[dict]:
        """Label unlabeled pool products for this persona and check AI reasons.
        Returns [{"model", "product_id"}] for unfaithful reasons."""
        known = labels.setdefault(persona["id"], {})
        ai_reasons = [(r["model"], p["product_id"], p["reason"]) for r in runs for p in r["picks"] if p["source"] == "ai"]
        if all(it.product.product_id in known for it in pool) and not ai_reasons:
            return []
        brief = (f"Recipient: {persona['recipient']}; occasion: {persona['occasion']}; budget band: "
                 f"{persona['budget_band']}; they are: {', '.join(persona['vibes'])}"
                 + (f"; age: {persona['age_band']}" if persona.get("age_band") else "")
                 + (f"; note: {persona['free_text']}" if persona.get("free_text") else ""))
        lines = [f"Shopper brief: {brief}", "", "Products:"]
        for it in pool:
            p = it.product
            lines.append(f"- {p.product_id} | {p.title} | ${p.price_min:.2f}"
                         + (f"-{p.price_max:.2f}" if p.price_max != p.price_min else "")
                         + f" | {p.product_type} | {strip_html(p.description)[:350]}")
        if ai_reasons:
            lines += ["", "Reasons to check (model | product_id | reason):"]
            lines += [f"- {m} | {pid} | {reason}" for m, pid, reason in ai_reasons]
        system = (
            "You are grading a gift finder. For the shopper brief, decide which listed products are genuinely "
            "good gifts: a thoughtful friend who knows the brief would happily suggest it, within budget and "
            "suitable for the recipient, occasion and age. Be strict about poor gifts (refills, spare parts, "
            "fees) and loose matches. Then check each reason: it is unfaithful if it states something not "
            "supported by that product's listing.\n"
            'Return JSON only: {"acceptable": [product_id, ...], "unfaithful": [{"model": ..., "product_id": ..., '
            '"why": "the unsupported claim, in a few words"}]}'
        )
        async with self.sem:
            self.ledger.check(EST_JUDGE_USD)
            # The judge may think (accuracy over speed); thinking counts toward max_tokens.
            resp = await self.chat_fn(model=JUDGE_MODEL, system=system, prompt="\n".join(lines),
                                      max_tokens=8000, temperature=0.0, json_mode=True, thinking=True,
                                      timeout=180.0)
        self.ledger.charge("labels", JUDGE_MODEL, resp.input_tokens, resp.output_tokens)
        try:
            data = json.loads(resp.text)
        except ValueError:
            self.log(f"[{store}] judge returned unparseable output for {persona['id']}; saved to results/judge_failures/")
            _write(self.results / "judge_failures" / f"{store}-{persona['id']}.json",
                   {"text": resp.text, "output_tokens": resp.output_tokens})
            return []
        acceptable = {str(x) for x in data.get("acceptable") or []}
        for it in pool:
            known.setdefault(it.product.product_id, it.product.product_id in acceptable)
        return [u for u in data.get("unfaithful") or [] if isinstance(u, dict)]

    # run ────────────────────────────────────────────────────────────────────
    async def run(self, personas: dict[str, list[dict]]) -> dict:
        outcomes: list[SearchOutcome] = []
        review_rows: list[dict] = []
        details: list[dict] = []  # every pick + reason + judge verdict, for inspecting failures
        overrides = _read(self.data / "label_overrides.json", {})
        for store in self.stores:
            products = await self.catalog(store)
            profiles = await self.profiles(store, products)
            items = await self.index(store, products, profiles)
            by_id = {it.product.product_id: it for it in items}
            has_5_types = len({it.product.product_type for it in items}) >= 5
            labels_path = self.data / "labels" / f"{store}.json"
            labels = _read(labels_path, {})
            try:
                for persona in personas[store]:
                    runs = await asyncio.gather(*(self.search(store, persona, m, items) for m in self.models))
                    intake = Intake(persona["recipient"], persona["occasion"], persona["budget_band"],
                                    persona["vibes"], persona.get("age_band"), persona.get("free_text", ""))
                    [qvec] = await self.embedder.embed([query_text(intake)])
                    pool_ids = {p["product_id"] for r in runs for p in r["picks"]}
                    pool_ids |= {c.product.product_id for c in retrieve(intake, items, qvec, limit=POOL_TOP_K)}
                    pool = [by_id[i] for i in sorted(pool_ids) if i in by_id]
                    unfaithful = await self.judge(store, persona, pool, runs, labels)
                    bad = {(u.get("model"), str(u.get("product_id"))): u.get("why", "") for u in unfaithful}
                    persona_labels = {**labels.get(persona["id"], {}),
                                      **overrides.get(store, {}).get(persona["id"], {})}
                    acceptable = {pid for pid, ok in persona_labels.items() if ok}
                    for r in runs:
                        details.append({
                            "store": store, "persona": persona, "model": r["model"], "mode": r["mode"],
                            "latency_ms": r["latency_ms"],
                            "picks": [{**p, "title": by_id[p["product_id"]].product.title if p["product_id"] in by_id else None,
                                       "acceptable": p["product_id"] in acceptable,
                                       "unfaithful": (r["model"], p["product_id"]) in bad,
                                       "why": bad.get((r["model"], p["product_id"]))}
                                      for p in r["picks"]],
                        })
                        picked = [p["product_id"] for p in r["picks"]]
                        ai = [p for p in r["picks"] if p["source"] == "ai"]
                        outcomes.append(SearchOutcome(
                            persona_id=persona["id"], catalog=store, model=r["model"], picked=picked,
                            acceptable=acceptable,
                            budget_violations=sum(1 for pid in picked if pid in by_id
                                                  and not in_budget(by_id[pid].product, persona["budget_band"])),
                            invented=sum(1 for pid in picked if pid not in by_id),
                            reasons_total=len(ai),
                            reasons_faithful=sum(1 for p in ai if (r["model"], p["product_id"]) not in bad),
                            distinct_types=len({by_id[pid].product.product_type for pid in picked[:5] if pid in by_id}),
                            catalog_has_5_types=has_5_types, latency_ms=r["latency_ms"],
                            input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                            used_fallback=r["used_fallback"],
                        ))
                    for pid, ok in persona_labels.items():
                        if random.random() < REVIEW_SAMPLE_RATE and pid in by_id:
                            p = by_id[pid].product
                            review_rows.append({"store": store, "persona": persona, "product_id": pid,
                                                "title": p.title, "price": p.price_min, "type": p.product_type,
                                                "description": strip_html(p.description)[:300], "judge_ok": ok})
            finally:
                _write(labels_path, labels)
        report = {"summary": summarize(outcomes), "by_store": self._by_store(outcomes),
                  "spend": {"total": round(self.ledger.spent, 4),
                            "by_step": {k: round(v, 4) for k, v in self.ledger.by_step().items()}}}
        _write(self.results / "report.json", report)
        _write(self.results / "outcomes.json", [asdict(o) | {"acceptable": sorted(o.acceptable)} for o in outcomes])
        _write(self.results / "searches.json", details)
        (self.results / "report.md").write_text(render_markdown(report))
        (self.results / "review.html").write_text(render_review(review_rows))
        return report

    @staticmethod
    def _by_store(outcomes: list[SearchOutcome]) -> dict:
        out: dict = {}
        for o in outcomes:
            out.setdefault(o.catalog, {}).setdefault(o.model, []).append(score_search(o)["relevant_at_5"])
        return {s: {m: round(sum(v) / len(v), 2) for m, v in ms.items()} for s, ms in out.items()}


def render_markdown(report: dict) -> str:
    lines = ["# Gift finder evaluation", "", "| Model | Searches | Good picks /5 | Over budget | Invented | "
             "Faithful reasons | Types in top 5 | p95 latency | Fallback | Cost/search | Pass |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for model, r in report["summary"].items():
        types = f"{r['avg_distinct_types']:.1f}" if r["avg_distinct_types"] is not None else "n/a"
        lines.append(
            f"| {model} | {r['searches']} | {r['avg_relevant_at_5']:.2f} | {r['budget_violations']} | "
            f"{r['invented']} | {r['faithfulness']:.0%} | {types} | {r['p95_latency_ms']} ms | "
            f"{r['fallback_rate']:.0%} | ${r['cost_per_search']:.4f} | {'✅' if r['passes'] else '❌'} |")
    lines += ["", "## Good picks /5 by store", ""]
    for store, models in report["by_store"].items():
        lines.append(f"- **{store}**: " + ", ".join(f"{m} {v}" for m, v in models.items()))
    spend = report["spend"]
    lines += ["", f"## Spend: ${spend['total']:.2f}", ""]
    lines += [f"- {k}: ${v:.4f}" for k, v in spend["by_step"].items()]
    return "\n".join(lines) + "\n"


def render_review(rows: list[dict]) -> str:
    """Spot-check page: mark any judge decision you disagree with, then copy the
    corrections JSON back (saved to evals/data/label_overrides.json)."""
    import html
    items = []
    for i, r in enumerate(rows):
        p = r["persona"]
        brief = f"{p['recipient']} · {p['occasion']} · {p['budget_band']} · {', '.join(p['vibes'])}" + (
            f" · “{p['free_text']}”" if p.get("free_text") else "")
        items.append(
            f'<tr><td>{html.escape(r["store"])}<br><small>{html.escape(p["id"])}</small></td>'
            f'<td>{html.escape(brief)}</td><td><b>{html.escape(r["title"])}</b> (${r["price"]:.2f}, '
            f'{html.escape(r["type"])})<br><small>{html.escape(r["description"])}</small></td>'
            f'<td>{"✅ good gift" if r["judge_ok"] else "❌ not a fit"}</td>'
            f'<td><input type="checkbox" data-i="{i}"> disagree</td></tr>')
    data = json.dumps([{"store": r["store"], "persona": r["persona"]["id"], "product_id": r["product_id"],
                        "judge_ok": r["judge_ok"]} for r in rows])
    return f"""<!doctype html><meta charset="utf-8"><title>GiftSense label spot-check</title>
<style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse}}td{{border:1px solid #ddd;padding:6px;vertical-align:top;max-width:420px}}</style>
<h1>Label spot-check ({len(rows)} sampled)</h1>
<p>Tick “disagree” where the judge is wrong, then click Copy and paste the result to Claude.</p>
<button onclick="copyOut()">Copy corrections</button> <span id="s"></span>
<table><tr><th>Store</th><th>Shopper</th><th>Product</th><th>Judge</th><th></th></tr>{''.join(items)}</table>
<script>const rows={data};
function copyOut(){{const out={{}};document.querySelectorAll('input:checked').forEach(c=>{{const r=rows[+c.dataset.i];
((out[r.store]??={{}})[r.persona]??={{}})[r.product_id]=!r.judge_ok}});
navigator.clipboard.writeText(JSON.stringify(out,null,1)).then(()=>document.getElementById('s').textContent='Copied');}}</script>"""
