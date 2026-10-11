#!/bin/sh
# Started by `shopify app dev --config dev --tunnel-url https://giftsense-dev.prudix.app:8001`.
# The CLI's proxy owns :8001 and forwards here on the CLI-provided PORT.
# 8001 is only the fallback for running the backend on its own.
# Reload only on backend code: watching the whole repo restarted the server on
# every theme/dashboard/test edit, and a webhook arriving mid-restart fails.
exec .venv/bin/uvicorn app.main:app --reload --reload-dir app --reload-dir core --host 0.0.0.0 --port "${PORT:-8001}"
