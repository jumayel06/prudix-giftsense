import { useState, useEffect } from 'react'
import { Page, Spinner, Banner } from '@shopify/polaris'
import { shopifyFetch, fetchJson } from '../utils/shopifyFetch'

// Adapted from Prudix Commerce's PlanPickerPage: monthly-only, and every
// feature row / label / AI option comes from /api/plans (app/config.py),
// so nothing here can drift from the backend.

const PLAN_ACCENTS = { starter: '#64748b', growth: '#eab308', pro: '#6366f1' }
const PLAN_RIBBONS = { starter: 'Best Value', growth: 'Most Versatile', pro: 'Most Complete' }

function ConfirmDialog({ plan, onConfirm, onCancel }) {
  if (!plan) return null
  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 9999, background: 'rgba(15, 23, 42, 0.5)',
      display: 'flex', alignItems: 'center', justifyContent: 'center', padding: '16px',
    }}>
      <div style={{
        background: 'white', borderRadius: '14px', padding: '28px 28px 24px',
        maxWidth: '420px', width: '100%', boxShadow: '0 20px 60px rgba(0,0,0,0.2)',
      }}>
        <p style={{ margin: '0 0 8px', fontSize: '16px', fontWeight: 700, color: '#1e293b' }}>
          End your trial early?
        </p>
        <p style={{ margin: '0 0 24px', fontSize: '14px', color: '#64748b', lineHeight: 1.6 }}>
          Switching to <strong>{plan.name}</strong> ends your trial now.
          You'll be billed <strong>${plan.price_usd}/month</strong> starting today.
        </p>
        <div style={{ display: 'flex', gap: '10px', justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{
            padding: '9px 18px', borderRadius: '8px', border: '1px solid #e2e8f0',
            background: 'white', color: '#64748b', fontSize: '13px', fontWeight: 600, cursor: 'pointer',
          }}>Keep trial</button>
          <button onClick={onConfirm} style={{
            padding: '9px 18px', borderRadius: '8px', border: 'none',
            background: '#1e293b', color: 'white', fontSize: '13px', fontWeight: 700, cursor: 'pointer',
          }}>Switch to {plan.name}</button>
        </div>
      </div>
    </div>
  )
}

function Limit({ label, value }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px' }}>
      <span style={{ fontSize: '12px', color: '#64748b' }}>{label}</span>
      <span style={{ fontSize: '12px', color: '#475569', fontWeight: 600 }}>{value}</span>
    </div>
  )
}

function PlanCard({ plan, catalog, recommended, trialUsed, currentTier, planStatus, trialDaysRemaining, onSelect, loading }) {
  const accent = PLAN_ACCENTS[plan.tier] || '#64748b'
  const isLoading = loading === plan.tier
  const isCurrent = plan.tier === currentTier
  const isTrialActive = isCurrent && planStatus === 'trial_active'
  const [hovered, setHovered] = useState(false)

  function ctaLabel() {
    if (isCurrent && isTrialActive) return 'Start paying now'
    if (isCurrent) return 'Current plan'
    if (!trialUsed && plan.trial_days > 0) return 'Start free trial'
    const hasActivePlan = currentTier && !['none', 'pending', 'uninstalled'].includes(currentTier)
    return hasActivePlan ? `Switch to ${plan.name}` : `Choose ${plan.name}`
  }

  const isDisabled = isLoading || (isCurrent && !isTrialActive)
  const ribbon = isTrialActive ? 'Trial active' : isCurrent ? 'Current plan' : PLAN_RIBBONS[plan.tier]

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        flex: '1 1 300px', minWidth: 0, background: 'white', borderRadius: '14px',
        border: isCurrent || hovered || recommended ? `2px solid ${accent}` : '2px solid #e2e8f0',
        boxShadow: isCurrent
          ? `0 0 0 4px ${accent}33, 0 10px 36px ${accent}55`
          : hovered ? `0 12px 40px ${accent}33, 0 2px 8px rgba(0,0,0,0.08)`
            : recommended ? `0 4px 24px ${accent}22` : '0 1px 6px rgba(0,0,0,0.05)',
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        transform: hovered && !isCurrent ? 'translateY(-4px)' : 'translateY(0)',
        transition: 'transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease',
        position: 'relative', zIndex: hovered ? 2 : 1,
      }}>
      <div style={{
        background: isTrialActive ? '#16a34a' : accent, color: 'white', textAlign: 'center',
        fontSize: '11px', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', padding: '6px 0',
      }}>{ribbon}</div>

      <div style={{ padding: '24px 24px 20px', flex: 1, display: 'flex', flexDirection: 'column' }}>
        <div style={{ marginBottom: '16px' }}>
          <p style={{ margin: '0 0 6px', fontSize: '13px', fontWeight: 700, color: accent, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            {plan.name}
          </p>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: '4px' }}>
            <span style={{ fontSize: '36px', fontWeight: 800, color: '#1e293b', lineHeight: 1 }}>
              ${plan.price_usd.toFixed(0)}
            </span>
            <span style={{ fontSize: '13px', color: '#94a3b8' }}>/month</span>
          </div>
        </div>

        <div style={{
          background: `${accent}10`, border: `1px solid ${accent}30`, borderRadius: '8px',
          padding: '10px 14px', marginBottom: '16px',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span style={{ fontSize: '13px', color: '#374151', fontWeight: 600 }}>
            {plan.generation_limit.toLocaleString()} AI generations/mo
          </span>
          {!trialUsed && !isCurrent && plan.trial_days > 0 && (
            <span style={{ fontSize: '11px', fontWeight: 700, color: '#16a34a', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: '6px', padding: '2px 8px' }}>
              Try free
            </span>
          )}
        </div>

        {isTrialActive ? (
          <p style={{ margin: '0 0 16px', fontSize: '12px', color: '#64748b' }}>
            Your trial includes <strong>{plan.trial_generations}</strong> AI generations.
            Click <em>Start paying now</em> to unlock the full {plan.generation_limit.toLocaleString()} today.
            {trialDaysRemaining > 0 && <> Or wait {trialDaysRemaining} day{trialDaysRemaining === 1 ? '' : 's'} and it converts automatically.</>}
          </p>
        ) : !trialUsed && !isCurrent && plan.trial_days > 0 ? (
          <p style={{ margin: '0 0 16px', fontSize: '12px', color: '#64748b' }}>
            {plan.trial_days}-day free trial with <strong>{plan.trial_generations}</strong> AI generations. No charge until day {plan.trial_days + 1}.
          </p>
        ) : null}

        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '20px' }}>
          {Object.entries(catalog.feature_categories).map(([category, features], i) => (
            <div key={category} style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <p style={{
                margin: i === 0 ? '0 0 2px' : '8px 0 2px', fontSize: '10px', fontWeight: 700, color: '#94a3b8',
                textTransform: 'uppercase', letterSpacing: '0.06em',
              }}>{category}</p>
              {features.map(f => (
                <div key={f} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' }}>
                  <span style={{ fontSize: '12px', color: '#64748b' }}>{catalog.feature_labels[f] || f}</span>
                  {plan.features.includes(f)
                    ? <span style={{ color: '#10b981', fontWeight: 700 }}>✓</span>
                    : <span style={{ fontSize: '13px' }}>🔒</span>}
                </div>
              ))}
            </div>
          ))}
          <p style={{ margin: '8px 0 2px', fontSize: '10px', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Monthly limits
          </p>
          <Limit label="Products in the gift finder" value={plan.max_products.toLocaleString()} />
          <Limit label="Voice / video messages" value={plan.media_messages_per_month ? plan.media_messages_per_month.toLocaleString() : '—'} />
        </div>

        <div style={{ marginBottom: '20px' }}>
          <p style={{ margin: '0 0 6px', fontSize: '11px', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            AI options
          </p>
          <div style={{ display: 'grid', gap: '6px' }}>
            {Object.entries(catalog.ai_tiers).map(([t, tier]) => {
              const included = plan.ai_tiers.includes(t)
              return (
                <div key={t} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '8px' }}>
                  <div>
                    <div style={{ fontSize: '12px', fontWeight: 600, color: included ? '#0f172a' : '#94a3b8' }}>{tier.label}</div>
                    <div style={{ fontSize: '11px', color: '#94a3b8', lineHeight: 1.4 }}>{tier.description}</div>
                  </div>
                  {included
                    ? <span style={{ color: '#10b981', fontWeight: 700 }}>✓</span>
                    : <span style={{ fontSize: '13px' }}>🔒</span>}
                </div>
              )
            })}
          </div>
        </div>

        <button
          onClick={() => !isDisabled && onSelect(plan.tier)}
          disabled={isDisabled}
          style={{
            width: '100%', padding: '12px',
            background: isCurrent && !isTrialActive ? '#f1f5f9'
              : recommended || isTrialActive || plan.tier === 'pro' ? accent : 'white',
            color: isCurrent && !isTrialActive ? '#94a3b8'
              : (recommended || isTrialActive || plan.tier === 'pro') ? 'white' : accent,
            border: `2px solid ${isCurrent && !isTrialActive ? '#e2e8f0' : accent}`,
            borderRadius: '9px', fontSize: '14px', fontWeight: 700,
            cursor: isDisabled ? 'default' : 'pointer',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px',
            opacity: isLoading ? 0.7 : 1,
          }}
        >
          {isLoading ? (
            <>
              <span style={{ display: 'inline-block', width: 16, height: 16, border: '2px solid currentColor', borderTopColor: 'transparent', borderRadius: '50%', animation: 'spin 0.7s linear infinite' }} />
              Redirecting…
            </>
          ) : ctaLabel()}
        </button>
      </div>
    </div>
  )
}

export default function PlanPickerPage() {
  const [catalog, setCatalog] = useState(null)
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(null)
  const [error, setError] = useState(null)
  const [confirmPlan, setConfirmPlan] = useState(null)

  useEffect(() => {
    Promise.all([
      fetch('/api/plans').then(r => (r.ok ? r.json() : null)),
      fetchJson('/api/stats').catch(() => null),
    ]).then(([plansData, statsData]) => {
      if (plansData) setCatalog(plansData)
      else setError('Failed to load plans. Please refresh.')
      setStats(statsData)
    }).catch(() => setError('Failed to load plans. Please refresh.'))
  }, [])

  const planStatus = stats?.plan_status ?? null
  const trialUsed = !!stats?.trial_used || planStatus === 'trial_active'
  const isOnboarding = planStatus === 'pending'
  const currentTier = isOnboarding ? null : (stats?.plan_tier ?? null)

  async function doSelect(tier) {
    setConfirmPlan(null)
    setLoading(tier)
    setError(null)
    try {
      const res = await shopifyFetch(`/api/billing/create-charge?plan=${tier}`, { method: 'POST' })
      if (!res.ok) throw new Error('Failed to create charge')
      const { confirmation_url } = await res.json()
      // Shopify's approval page refuses to render inside the admin iframe —
      // navigate the top-level window instead.
      window.top.location.href = confirmation_url
    } catch {
      setError('Something went wrong. Please try again.')
      setLoading(null)
    }
  }

  async function handleSelect(tier) {
    if (planStatus === 'trial_active' && tier !== stats?.plan_tier) {
      setConfirmPlan(catalog.plans.find(p => p.tier === tier))
      return
    }
    await doSelect(tier)
  }

  if (!catalog) {
    return (
      <Page title="Choose a plan">
        {error
          ? <Banner tone="critical" title={error} />
          : <div style={{ display: 'flex', justifyContent: 'center', padding: '80px' }}><Spinner size="large" /></div>}
      </Page>
    )
  }

  return (
    <Page
      title={isOnboarding ? 'Welcome to Prudix GiftSense' : 'Choose a plan'}
      subtitle={isOnboarding
        ? (trialUsed
            ? 'Pick a plan to get started. The free trial was already used on this store, so billing begins today.'
            : 'Pick a plan to get started. 7-day free trial, no charge until it ends.')
        : 'Upgrade or cancel anytime. Downgrades take effect at the end of your billing month.'}
    >
      <style>{'@keyframes spin { to { transform: rotate(360deg) } }'}</style>

      <ConfirmDialog plan={confirmPlan} onConfirm={() => doSelect(confirmPlan.tier)} onCancel={() => setConfirmPlan(null)} />

      {error && <div style={{ marginBottom: '20px' }}><Banner tone="critical" title={error} /></div>}

      {stats?.scheduled_plan_name && stats?.scheduled_change_at && (
        <div style={{ marginBottom: '20px' }}>
          <Banner tone="info" title={`Your plan changes to ${stats.scheduled_plan_name} on ${new Date(stats.scheduled_change_at).toLocaleDateString()}.`} />
        </div>
      )}

      <div className="prudix-plan-cards" style={{ display: 'flex', gap: '16px', alignItems: 'flex-start', flexWrap: 'wrap' }}>
        {catalog.plans.map(plan => (
          <PlanCard
            key={plan.tier}
            plan={plan}
            catalog={catalog}
            recommended={plan.tier === 'growth' && !currentTier}
            trialUsed={trialUsed}
            currentTier={currentTier}
            planStatus={planStatus}
            trialDaysRemaining={stats?.trial_days_remaining ?? null}
            onSelect={handleSelect}
            loading={loading}
          />
        ))}
      </div>

      <div style={{ marginTop: '24px', fontSize: '12px', color: '#64748b', lineHeight: 1.6 }}>
        Each AI gift search, note or registry suggestion uses your monthly generations; stronger AI options use more per use. You choose the option in Settings.
        When a limit is reached, the gift finder and notes keep working in basic mode.
      </div>
    </Page>
  )
}
