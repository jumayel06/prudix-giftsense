/**
 * Display labels for LLM model IDs. Keep in sync with `MODEL_WEIGHTS` in
 * `app/config.py`. Weights come from the API (`/api/plans`, `/api/settings`),
 * never hardcoded here, so they can't drift (a Commerce tech-debt item).
 */
export const MODEL_LABELS = {
  'gpt-4o-mini':      'GPT-4o mini',
  'claude-haiku-4-5': 'Claude Haiku 4.5',
  'gpt-4.1':          'GPT-4.1',
  'claude-sonnet-5':  'Claude Sonnet 5',
}

export function modelLabel(modelId) {
  if (!modelId) return null
  return MODEL_LABELS[modelId] || modelId
}
