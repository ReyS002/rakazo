# App icon source

`bull-desk-logo.png` is the Bull Desk logo and the single source for every app icon: a
512-pixel square with the bull in a gold ring on its own navy plate.

`Rakazo.icon` is the Icon Composer package that macOS and iOS packaging read. Its one layer,
`Assets/orange-bot.png`, keeps its upstream name because the build and CI look the layer up by
name; the export script fills it with the Bull Desk logo, centred and full-bleed.

Run `pnpm --filter @rakazo/ui-tokens export:brand` after changing the logo. It refreshes:

- web and marketing favicons, install icons, and the marketing mark (the logo cropped to its
  round badge for small marks, full-bleed for install icons);
- `apps/desktop/assets/icon.png`, `icon.ico`, and `icon-macos.png` (the 824-pixel macOS plate
  inside Dock margins);
- `apps/mobile/assets/icon.png`, `favicon.png`, and `splash-icon.png`;
- the Android adaptive layers: `adaptive-icon.png` (the badge inside the launcher's safe
  circle), `icon-background.png` (the dark background token), and `monochrome-icon.png` plus
  `notification-icon.png` (white silhouettes on transparent);
- the marketing social cards in `apps/www/public/og-image.png` and `apps/www/public/og/`.

Android supplies its own mask; never bake Mac corners, a rim, or an outer shadow into adaptive
layers.
