/**
 * Dashboard sections that are planned but not built yet. They show in the
 * sidebar with a "Soon" badge and open ComingSoonPage. Feature keys match
 * `features` in /api/plans (app/config.py), so "which plans include it" is
 * never hardcoded here. Remove an entry when its real page ships.
 */
import {
  CalendarIcon, MicrophoneIcon, ReplaceIcon,
  ListBulletedIcon,
} from '@shopify/polaris-icons'

export const UPCOMING = [
  {
    path: '/delivery', label: 'Arrive-by dates', section: 'gifting', icon: CalendarIcon,
    title: 'Arrive-by dates',
    intro: 'Let shoppers pick when a gift should arrive, for birthdays and holidays.',
    items: [{ feature: 'arrive_by', text: 'Delivery-date rules and automatic shipping holds until it is time to send' }],
  },
  {
    path: '/messages', label: 'Voice & video', section: 'gifting', icon: MicrophoneIcon,
    title: 'Voice and video messages',
    intro: 'A recorded message the recipient opens by scanning a QR code on the gift note.',
    items: [
      { feature: 'voice_messages', text: 'Voice messages' },
      { feature: 'video_messages', text: 'Video messages' },
    ],
  },
  {
    path: '/choice', label: "Recipient's choice", section: 'gifting', icon: ReplaceIcon,
    title: "Recipient's choice",
    intro: 'The recipient can swap size or color before it ships, so fewer exchanges.',
    items: [{ feature: 'recipients_choice', text: 'Same-price swaps with a deadline you set' }],
  },
  {
    path: '/registries', label: 'Registries', section: 'gifting', icon: ListBulletedIcon,
    title: 'Gift registries',
    intro: 'Customers build a wish list from your store and share it; every share brings new shoppers.',
    items: [{ feature: 'registries', text: 'Registries with AI suggestions and purchase tracking' }],
  },
]

export const UPCOMING_BY_PATH = Object.fromEntries(UPCOMING.map(u => [u.path, u]))
