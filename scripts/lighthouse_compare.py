#!/usr/bin/env python3
"""Lighthouse budget check: GiftSense may cost at most 10 performance points
(docs/TECHNICAL_PLAN.md §12, week 9).

Runs Lighthouse (mobile) on the same storefront page twice: normally, and with
every GiftSense asset blocked (--blocked-url-patterns), so the difference is
exactly what GiftSense adds. Median of N runs each; exits 1 when the drop is
over the budget.

    .venv/bin/python scripts/lighthouse_compare.py https://prudix-commerce-dev.myshopify.com/products/x \\
        --cookie "storefront_digest=…"      # password-protected dev stores (copy it from DevTools)

Needs Node (npx) and Chrome. Nothing is sent anywhere except the page loads.
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

BUDGET = 10
BLOCK = ["*giftsense*", "*/apps/giftsense/*"]


def run(url: str, cookie: str | None, block: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        cmd = ["npx", "--yes", "lighthouse", url, "--quiet", "--output=json", f"--output-path={out}",
               "--only-categories=performance", "--chrome-flags=--headless=new"]
        if cookie:
            cmd.append("--extra-headers=" + json.dumps({"Cookie": cookie}))
        if block:
            cmd += [f"--blocked-url-patterns={p}" for p in BLOCK]
        subprocess.run(cmd, check=True, capture_output=True)
        report = json.loads(out.read_text())
    audits = report["audits"]
    return {"score": round(report["categories"]["performance"]["score"] * 100),
            "lcp_ms": audits["largest-contentful-paint"]["numericValue"],
            "tbt_ms": audits["total-blocking-time"]["numericValue"],
            "bytes": audits["total-byte-weight"]["numericValue"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--cookie", help="Cookie header, e.g. storefront_digest=… for password-protected stores")
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    results = {}
    for label, block in (("with GiftSense", False), ("without GiftSense", True)):
        runs = []
        for i in range(args.runs):
            print(f"{label}: run {i + 1}/{args.runs}…", flush=True)
            runs.append(run(args.url, args.cookie, block))
        results[label] = {k: statistics.median(r[k] for r in runs) for k in runs[0]}

    on, off = results["with GiftSense"], results["without GiftSense"]
    drop = off["score"] - on["score"]
    print(f"\n{'':20}{'score':>8}{'LCP ms':>10}{'TBT ms':>10}{'KB':>10}")
    for label, r in results.items():
        print(f"{label:20}{r['score']:>8.0f}{r['lcp_ms']:>10.0f}{r['tbt_ms']:>10.0f}{r['bytes'] / 1024:>10.0f}")
    print(f"\nGiftSense costs {drop:.0f} performance point(s); budget {BUDGET}. "
          f"{'OK' if drop <= BUDGET else 'OVER BUDGET'}")
    return 0 if drop <= BUDGET else 1


if __name__ == "__main__":
    sys.exit(main())
