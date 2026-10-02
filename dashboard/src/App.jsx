import { useState, useEffect, useCallback } from 'react'
import { flushSync } from 'react-dom'
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { AppProvider, Frame, Navigation, SkeletonPage, SkeletonBodyText, Badge } from '@shopify/polaris'
import { TitleBar } from '@shopify/app-bridge-react'
import {
  HomeIcon, CreditCardIcon, SettingsIcon, ChatIcon, ProductIcon, WandIcon, StoreIcon, ChartVerticalIcon,
  NoteIcon, PackageIcon, OrderIcon,
} from '@shopify/polaris-icons'
import enTranslations from '@shopify/polaris/locales/en.json'
import '@shopify/polaris/build/esm/styles.css'

import { shopifyFetch, fetchJson } from './utils/shopifyFetch'
import HomePage from './pages/HomePage'
import CatalogPage from './pages/CatalogPage'
import PlaygroundPage from './pages/PlaygroundPage'
import PlanPickerPage from './pages/PlanPickerPage'
import SettingsPage from './pages/SettingsPage'
import SupportPage from './pages/SupportPage'
import ComingSoonPage from './pages/ComingSoonPage'
import StorefrontPage from './pages/StorefrontPage'
import AnalyticsPage from './pages/AnalyticsPage'
import GiftNotesPage from './pages/GiftNotesPage'
import GiftWrapPage from './pages/GiftWrapPage'
import GiftOrdersPage from './pages/GiftOrdersPage'
import { UPCOMING } from './utils/upcoming'
import { navBadge } from './utils/planBadge'
import ThemeWarningBanner from './components/ThemeWarningBanner'

const APP_NAME = 'Prudix GiftSense'

function PrudixLogo() {
  return (
    <>
      <svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 32 32">
        <path fill="none" stroke="#eab308" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"
          d="M15 27 L12 4 L6 4 Q1 4 1 11 Q1 18 6 18 L14 18"/>
        <circle cx="17.5" cy="15" r="1.8" fill="#eab308"/>
        <g transform="rotate(15, 24, 23)">
          <path fill="none" stroke="#1e293b" strokeWidth="2.2" strokeLinecap="round"
            d="M20 19 L28 27 M28 19 L20 27"/>
        </g>
      </svg>
      <span className="prudix-sidebar-brand-text" style={{
        fontFamily: "'Poppins', sans-serif", fontWeight: 800,
        fontSize: '22px', letterSpacing: '-0.02em',
        background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 55%, #eab308 100%)',
        WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent',
        backgroundClip: 'text', color: '#0f172a',
      }}>
        GiftSense
      </span>
    </>
  )
}

function SidebarNav({ planStatus, planTier, onMobileClose }) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [plans, setPlans] = useState(null)

  useEffect(() => { fetchJson('/api/plans').then(d => setPlans(d.plans)).catch(() => setPlans(null)) }, [])

  // Hide nav while the merchant is on the plan picker (no plan yet).
  if (planStatus === 'pending') return null

  // Close the mobile sheet on navigation (instead of a setState-in-effect).
  const badge = (feature, soon) => {
    const b = navBadge({ plans, planTier, feature, soon })
    if (!b) return undefined
    if (typeof b === 'string') return b
    return <Badge tone="info" size="small">{b.soon ? `${b.plan} · Soon` : b.plan}</Badge>
  }
  // `feature` (a PLANS feature key) adds a plan badge when the merchant's plan lacks it.
  const item = (label, path, icon, feature) => ({
    label, icon, selected: pathname === path, badge: badge(feature, false),
    onClick: () => { onMobileClose(); navigate(path) },
  })
  // Planned sections (utils/upcoming.js): shown with a "Soon" badge until they ship.
  const soon = section => UPCOMING.filter(u => u.section === section)
    .map(u => ({ ...item(u.label, u.path, u.icon), badge: badge(u.items[0]?.feature, true) }))

  return (
    <div className="prudix-sidebar-wrapper">
      <button type="button" className="prudix-sidebar-close" onClick={onMobileClose} aria-label="Close menu">✕</button>
      <div className="prudix-sidebar-brand" onClick={() => navigate('/')}>
        <PrudixLogo />
      </div>
      <div className="prudix-nav-scroll">
        <Navigation location={pathname}>
          <Navigation.Section items={[item('Home', '/', HomeIcon)]} />
          <Navigation.Section
            title="Gift finder"
            items={[item('Catalog', '/catalog', ProductIcon), item('Try it', '/playground', WandIcon), item('Storefront', '/storefront', StoreIcon)]}
          />
          <Navigation.Section
            title="Gifting"
            items={[item('Gift notes', '/notes', NoteIcon, 'ai_notes'), item('Gift wrap', '/wrap', PackageIcon, 'gift_wrap'), ...soon('gifting')]}
          />
          <Navigation.Section title="Orders" items={[item('Gift orders', '/orders', OrderIcon, 'gift_cards_print')]} />
          <Navigation.Section title="Insights" items={[item('Analytics', '/analytics', ChartVerticalIcon)]} />
          <Navigation.Section
            title="Account"
            items={[
              item('Plans', '/plans', CreditCardIcon),
              item('Settings', '/settings', SettingsIcon),
            ]}
          />
          <Navigation.Section separator items={[item('Support', '/support', ChatIcon)]} />
        </Navigation>
      </div>
    </div>
  )
}

function AppFooter() {
  const year = new Date().getFullYear()
  return (
    <footer className="prudix-app-footer">
      <div className="prudix-app-footer-inner">
        <div className="prudix-app-footer-brand">
          <span className="prudix-app-footer-brand-text">
            PrudiX<sup className="prudix-app-footer-tm">™</sup>
          </span>
        </div>
        <div className="prudix-app-footer-right">
          <span className="prudix-app-footer-email">support@prudix.app</span>
          <span className="prudix-app-footer-sep" aria-hidden="true">·</span>
          <a href="https://prudix.app" target="_blank" rel="noreferrer">Website</a>
          <span className="prudix-app-footer-sep" aria-hidden="true">·</span>
          <a href="https://prudix.app/privacy" target="_blank" rel="noreferrer">Privacy</a>
          <span className="prudix-app-footer-sep" aria-hidden="true">·</span>
          <a href="https://prudix.app/terms" target="_blank" rel="noreferrer">Terms</a>
          <span className="prudix-app-footer-sep" aria-hidden="true">·</span>
          <span className="prudix-app-footer-copyright">© {year} Prudix</span>
        </div>
      </div>
    </footer>
  )
}

const TITLES = {
  '/': APP_NAME, '/catalog': 'Catalog', '/playground': 'Try the gift finder', '/plans': 'Plans', '/settings': 'Settings', '/support': 'Support', '/storefront': 'Storefront', '/analytics': 'Analytics', '/notes': 'Gift notes', '/wrap': 'Gift wrap', '/orders': 'Gift orders',
  ...Object.fromEntries(UPCOMING.map(u => [u.path, u.title])),
}

function AppShell() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [showMobileNav, setShowMobileNav] = useState(false)
  const planStatus = stats?.plan_status ?? null

  // Reset scroll on route change (same as Commerce).
  useEffect(() => {
    try { window.scrollTo({ top: 0, behavior: 'instant' }) } catch { /* ignore */ }
    for (const sel of ['.Polaris-Frame__Main', '.prudix-route-container']) {
      const el = document.querySelector(sel)
      if (el) el.scrollTop = 0
    }
  }, [pathname])

  // No sidebar on the plan picker during onboarding → drop Main's left gutter.
  useEffect(() => {
    document.body.classList.toggle('prudix-no-nav', planStatus === 'pending')
    return () => { document.body.classList.remove('prudix-no-nav') }
  }, [planStatus])

  const dismissMobileNav = useCallback(() => {
    flushSync(() => setShowMobileNav(false))
  }, [])

  // Boot: /api/stats provisions the shop on first load (session-token exchange
  // in get_current_shop) and tells us where to route. Pending → plan picker.
  useEffect(() => {
    shopifyFetch('/api/stats')
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        if (d) {
          setStats(d)
          if (d.plan_status === 'pending' && pathname !== '/plans') {
            navigate('/plans', { replace: true })
          }
        }
        setLoading(false)
      })
      .catch(() => setLoading(false))
    // Mount only: the billing callback redirects back into the app, which reloads.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (loading) {
    return (
      <Frame>
        <SkeletonPage>
          <SkeletonBodyText lines={4} />
        </SkeletonPage>
      </Frame>
    )
  }

  const trialCap = stats?.trial_generations_cap || 0
  const trialExhausted = planStatus === 'trial_active' && trialCap > 0
    && (stats?.trial_generations_used || 0) >= trialCap

  return (
    <Frame
      navigation={planStatus === 'pending'
        ? undefined
        : <SidebarNav planStatus={planStatus} planTier={stats?.plan_tier} onMobileClose={dismissMobileNav} />}
      showMobileNavigation={showMobileNav}
      onNavigationDismiss={dismissMobileNav}
    >
      <TitleBar title={TITLES[pathname] || APP_NAME} />

      <button
        type="button"
        className="prudix-mobile-nav-trigger"
        data-nav-open={showMobileNav ? 'true' : 'false'}
        aria-label="Open menu"
        onClick={() => setShowMobileNav(true)}
      >
        ☰
      </button>
      {trialExhausted && pathname !== '/plans' && (
        <div className="prudix-trial-banner prudix-trial-banner--critical">
          <span className="prudix-trial-banner-icon">🚫</span>
          <span className="prudix-trial-banner-text">
            Trial AI limit reached. The gift finder and notes keep working in basic mode; choose a plan for full AI.
          </span>
          <button className="prudix-trial-banner-cta" onClick={() => navigate('/plans')}>
            Choose a plan →
          </button>
        </div>
      )}
      <div className="prudix-app-body">
        {stats?.theme_warning && planStatus !== 'pending' && (
          <ThemeWarningBanner warning={stats.theme_warning} shop={stats.shop_domain} onNavigate={navigate} />
        )}
        <div className="prudix-route-container">
          <Routes>
            <Route path="/" element={<HomePage stats={stats} />} />
            <Route path="/catalog" element={<CatalogPage />} />
            <Route path="/storefront" element={<StorefrontPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/notes" element={<GiftNotesPage />} />
            <Route path="/wrap" element={<GiftWrapPage />} />
            <Route path="/orders" element={<GiftOrdersPage />} />
            <Route path="/playground" element={<PlaygroundPage />} />
            <Route path="/plans" element={<PlanPickerPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/support" element={<SupportPage />} />
            {UPCOMING.map(u => (
              <Route key={u.path} path={u.path} element={<ComingSoonPage planTier={stats?.plan_tier} />} />
            ))}
          </Routes>
        </div>
        {planStatus !== 'pending' && <AppFooter />}
      </div>
    </Frame>
  )
}

export default function App() {
  return (
    <AppProvider i18n={enTranslations}>
      <BrowserRouter>
        <AppShell />
      </BrowserRouter>
    </AppProvider>
  )
}
