// Theme editor deep links (the editor lives outside the embedded app, so it
// opens in the top window). Block handles: extensions/giftsense-theme/blocks/*.liquid.
export const EMBED_HANDLE = 'app-embed'
export const BLOCK_HANDLE = 'gift-finder'
export const OPTIONS_HANDLE = 'gift-options'

export function editorUrl(shop, params) {
  return `https://${shop}/admin/themes/current/editor?${new URLSearchParams(params)}`
}

export function openThemeEditor(shop, params) {
  window.open(editorUrl(shop, params), '_top')
}

// Opens App embeds with GiftSense ready to switch on.
export function openEmbedSwitch(shop, apiKey) {
  openThemeEditor(shop, { context: 'apps', activateAppId: `${apiKey}/${EMBED_HANDLE}` })
}
