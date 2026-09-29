# Note for Prudix Commerce: AI tiers and graceful model switching (built in GiftSense)

GiftSense built Commerce's "Graceful model-switch architecture" item (PROD_RELEASE_CHECKLIST, *After App Store approval*) end to end, with tests. When Commerce does it, copy the code instead of designing it again. Every file below is in `~/Shopify/prudix-giftsense`.

## What was built

| Commerce plan item | GiftSense implementation |
|---|---|
| (1) Tiers, not model IDs | `AI_TIERS` in `app/ai_models.py`: **Standard / Advanced / Premium**, generations per use **1 / 2 / 4**. `Shop.selected_model` stores the tier. `PLANS[...]["ai_tiers"]` and `PLAN_DEFAULT_AI_TIER` live in `app/config.py`. |
| (2) Model registry | `MODELS` in `app/ai_models.py`. Each entry has label, provider, `price` (USD per MTok, which replaces the hand-written `MODEL_COSTS`), capability flags (`caps`), `status`, `replacement`, `fallback` and `provider_retires_on`. |
| (3) Slots | `SLOTS`: `ai_standard`, `ai_advanced`, `ai_premium`, `catalog_analysis`, each shaped `{model, next, rollout_pct}`. Commerce would add its background slots here: Concierge KB build and live answers, the Sales Leak models, narratives, and the kit defaults. |
| (4) `resolve_model(slot, shop_id, pins)` | A per-shop admin pin wins, unless the pinned model is retired (`Shop.model_pins` JSON). Otherwise a sticky bucket, `sha256(f"{slot}:{shop_id}") % 100 < rollout_pct`, picks `next`. A retired model follows its `replacement` chain to an active model. `model_for_shop(shop)` resolves a shop's tier to its model. |
| (5) Request shaping from flags | `app/llm.py` reads `caps`: `max_tokens_param`, `temperature` (`always` / `never` / `thinking_off`), `thinking_off` and `thinking_on` extra fields. It keeps only the `type == "text"` blocks. |
| (6) Runtime fallback | `chat()` catches `anthropic.NotFoundError` and `openai.NotFoundError`, retries once on `replacement` or `fallback`, and logs `llm_model_gone_fallback` as an error. `LLMResponse.model` records which model answered. |
| (7) CI tripwire | `tests/unit/test_ai_models.py::test_no_model_in_use_is_near_its_retirement` fails when any model a slot (or its fallback) can run is within 60 days of `provider_retires_on`. |
| (8) Record the actual model | `usage_logs.model_used` stores the model that answered, which may be a fallback. Retired models stay in `MODELS` so historical costs still compute. |

**Migrations:** `20260928000003_ai_tiers.py` maps stored model IDs to tiers and adds `model_pins`. `20260928000004_rename_ai_tiers.py` is only needed because GiftSense first shipped the names Fast/Balanced. `LEGACY_SELECTIONS` maps any old stored value at read time as well.

## Swap procedure (the whole point)
1. Add the model to `MODELS`.
2. Set it as the slot's `next` and move `rollout_pct` through 5, 25 and 100. Watch quality and cost in `usage_logs`. Setting it back to 0 is an instant rollback.
3. Promote it to `model`, then mark the old model `retired` with a `replacement`.

No data migration and no listing change are needed. Each tier's weight is fixed, so a new model has to fit the tier's cost per generation.

## Product decisions worth copying
- **Tier names:** GiftSense started with Fast / Balanced / Premium, but "Fast" read as the best choice, so merchants would stick with the entry option. It was renamed to **Standard / Advanced / Premium**, with descriptions about quality rather than speed.
- **Defaults:** each plan starts on its best tier (Starter → Standard, Growth → Advanced, Pro → Premium). Most merchants never change it.
- **Transparency:** each store's own screens (Settings, the Home usage card, the test area) show "Currently runs on Claude Sonnet 5", resolved for that store so it stays correct during a rollout. Screens shown before signup (`/api/plans`, the plan picker, the App Store listing) name only the tiers, and a test enforces that `/api/plans` contains no model names.
- **Numbers:** Settings and the plan picker say only "stronger options use more generations". The exact per-use number appears on the usage card.
- **Privacy policy:** it must name OpenAI and Anthropic as subprocessors.

## API gotchas found on 2026-09-28 (these apply to Commerce's model refresh too)
- **Claude Sonnet 5 / 5.5** reject a non-default `temperature`, `top_p` or `top_k` with a 400. Both think by default.
  - Sonnet 5 turns thinking off with `thinking: {"type": "disabled"}`.
  - **Sonnet 5.5 rejects `disabled`.** Use `thinking: {"type": "between_tools"}` instead. In a request without tools, that means no thinking.
- **anthropic SDK ≥ 1.0 removes `temperature` entirely.** Passing it raises a `TypeError`, and GiftSense is still on 0.52.0. Drive temperature from `caps` before bumping the SDK.
- **GPT-6 (Luna / Sol)** are reasoning models:
  - they need `max_completion_tokens` instead of `max_tokens`;
  - send `reasoning_effort: "none"` for speed;
  - `temperature` is accepted only when effort is `none`.
  - openai 1.77.0 passes these through fine.
- **Claude Haiku 4.5**: Anthropic lists it as Active, with retirement *not before* 2026-10-15 and at least 60 days' notice. There is no newer Haiku.
- **Hidden thinking was the biggest latency cost:** Sonnet 5 with thinking on took 7.3 s at p95 on a short JSON task.

## Eval numbers behind the GiftSense choice
Setup: gift-finder search, 400-product store, 10 searches per model, Sonnet 5 as judge.

| Model | Good picks /5 | Faithful reasons | Template-fallback reasons | p95 | $/search |
|---|---|---|---|---|---|
| **GPT-6 Luna** | 3.25 | 97% | 8% | **2.8 s** | **$0.0003** |
| **GPT-6 Sol** | 3.23 | **100%** | 2% | 4.7 s | $0.0055 |
| **Claude Sonnet 5** | **3.50** | 95% | 20% | 6.0 s | $0.0103 |
| Claude Sonnet 5.5 | 3.10 | 93% | 10% | 4.9 s | $0.0106 |
| GPT-4o-mini | 3.43 | 96% | 29% | 4.6 s | $0.0004 |
| Claude Haiku 4.5 | 3.25 | 89% | 30% | 5.1 s | $0.0037 |
| GPT-4.1 | 3.20 | 92% | 28% | 3.6 s | $0.0058 |

**Takeaways for Commerce:**
- Luna is a strong replacement for GPT-4o-mini and Haiku.
- Sol is a good fit where accuracy matters, and it costs about half of Sonnet.
- Sonnet 5.5 has the same price as Sonnet 5, runs about 20% faster, and showed no quality gain on this task.
- Product-profile extraction on Luna cost $0.00014 per product, about 12× cheaper than Haiku. Grounded accuracy downstream came out a little lower (89% vs 97%, small sample), so test on Commerce's own prompts before switching.
- Measure on real Commerce prompts before flipping defaults (ad copy, FAQ, descriptions, blog, translate). Commerce's checklist item (10) still applies.

## Files to copy
- `app/ai_models.py`
- `app/llm.py`
- `tests/unit/test_ai_models.py`
- `tests/unit/test_llm.py` (the capability and fallback tests)
- the settings and stats routes (`ai_tier`, `ai_tier_models`, `ai_model_label`)
- `dashboard/src/pages/SettingsPage.jsx` (the AI section)
- `dashboard/src/pages/PlanPickerPage.jsx` (the "AI options" block)
- the two migrations above
