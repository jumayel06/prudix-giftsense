#!/bin/bash
# PostToolUse(Bash) — fires after every Bash command. Filters down to the
# full pytest suite (`pytest tests/` followed by space or EOL) and only
# injects a reminder when the run was green (pytest's "N passed" footer
# present, no "N failed").
#
# Reads PostToolUse JSON on stdin. Emits JSON with hookSpecificOutput.additionalContext
# on a match, or exits 0 silently otherwise. Never blocks (no exit 2).
set -euo pipefail

INPUT="$(cat)"

# Pull the bash command. Empty string if the hook fires on a non-Bash tool
# (which shouldn't happen given the matcher, but defensive).
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Match: full-suite pytest invocations only.
#   .venv/bin/pytest tests/         ← match
#   pytest tests/ -x                ← match
#   pytest tests/ --tb=short -q     ← match
#   pytest tests/integration/test_x.py  ← skip (targeted run)
#   pytest tests                    ← skip (no slash, ambiguous)
if ! echo "$CMD" | grep -qE 'pytest[[:space:]]+tests/([[:space:]]|$)'; then
  exit 0
fi

# Pull stdout + stderr from the tool response. Pytest writes its summary
# line to stdout. Different tool implementations expose the field under
# different keys, so we coalesce.
OUT=$(echo "$INPUT" | jq -r '
  (.tool_response.stdout    // "")
  + " "
  + (.tool_response.stderr  // "")
  + " "
  + (.tool_response.output  // "")
  + " "
  + (.tool_response         | tostring)
')

# Green only if we see "<N> passed" AND no "<N> failed".
if echo "$OUT" | grep -qE '[0-9]+ passed' && ! echo "$OUT" | grep -qE '[0-9]+ failed'; then
  cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Full pytest suite ran green. Per CLAUDE.md, finish this response with a \"Next 5 pending tasks\" section pulled from LAUNCH_TODO.md — list 5 open `[ ]` items prioritized by current pre-launch context (single test store, no live merchants), formatted as a numbered list with the source section name in parens."}}
JSON
fi
