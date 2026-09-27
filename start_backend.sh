#!/bin/sh
# Started by `shopify app dev --config dev` (shopify.web.toml → port 8001).
# Uses the CLI-provided PORT so the CLI's proxy and webhook self-check reach it.
exec .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port "${PORT:-8001}"
