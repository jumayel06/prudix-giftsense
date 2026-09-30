/**
 * Dashboard sections that are planned but not built yet. They show in the
 * sidebar with a "Soon" badge and open ComingSoonPage. Feature keys match
 * `features` in /api/plans (app/config.py), so "which plans include it" is
 * never hardcoded here. Remove an entry when its real page ships.
 */
import {
  NoteIcon, CalendarIcon, MicrophoneIcon, ReplaceIcon,
  ListBulletedIcon, OrderIcon,
} from '@shopify/polaris-icons'

export const UPCOMING = [
  {
    path: '/notes', label: 'Gift notes', section: 'gifting', icon: NoteIcon,
    title: 'AI gift notes',
    intro: 'Shoppers write a personal note with a little AI help, and it prints on a gift card.',
    items: [
      { feature: 'ai_notes', text: 'AI drafts in a tone the shopper picks, with your banned words respected' },
      { feature: 'gift_groups', text: 'Several gifts in one order, each with its own recipient and note' },
    ],
  },
  {
    path: '/delivery', label: 'Arrive-by dates', section: 'gifting', icon: CalendarIcon,
    title: 'Arrive-by dates',
    intro: 'Let shoppers pick when a gift should arrive, for birthdays and holidays.',
    items: [{ feature: 'arrive_by', text: 'Delivery-date rules and automatic shipping holds until it is time to send' }],
  },
  {
    path: '/messages', label: 'Voice & video', section: 'gifting', icon: MicrophoneIcon,
    title: 'Voice and video messages',
    intro: 'A recorded message the recipient opens by scanning a QR code on the gift card.',
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
  {
    path: '/orders', label: 'Gift orders', section: 'orders', icon: OrderIcon,
    title: 'Gift orders',
    intro: 'Every gift order in one place, ready to pack.',
    items: [
      { feature: 'gift_cards_print', text: 'Print gift cards and tags for each gift' },
      { feature: 'gift_receipt', text: 'Price-free gift receipts' },
    ],
  },
]

export const UPCOMING_BY_PATH = Object.fromEntries(UPCOMING.map(u => [u.path, u]))
