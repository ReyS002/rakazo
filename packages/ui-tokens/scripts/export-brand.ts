import { mkdir, readdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import { darkTokens } from "../src/index.ts";

const root = new URL("../../../", import.meta.url);
const path = (name: string) => fileURLToPath(new URL(name, root));
const source = path("packages/ui-tokens/assets/bull-desk-logo.png");

// The logo is a full-bleed square: the bull in a gold ring on its own navy plate.
const square = await sharp(source).resize(1024, 1024, { kernel: "lanczos3" }).png().toBuffer();

function maskSvg(shape: string) {
  return Buffer.from(
    `<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024">${shape}</svg>`,
  );
}
async function masked(input: Buffer, shape: string) {
  return sharp(input)
    .ensureAlpha()
    .composite([{ input: maskSvg(shape), blend: "dest-in" }])
    .png()
    .toBuffer();
}
const rounded = await masked(square, '<rect width="1024" height="1024" rx="200" fill="white"/>');
// The ring's glow ends just inside this circle, so the badge stands alone on any colour.
const badge = await masked(square, '<circle cx="512" cy="512" r="490" fill="white"/>');

async function png(input: Buffer, name: string, size: number) {
  await sharp(input)
    .resize(size, size, { kernel: "lanczos3" })
    .ensureAlpha()
    .png()
    .toFile(path(name));
}

// PNG frames in ICO are supported by modern Windows and browser favicon loaders.
async function ico(input: Buffer, name: string, sizes: number[]) {
  const frames = await Promise.all(sizes.map((size) => sharp(input).resize(size).png().toBuffer()));
  const header = Buffer.alloc(6 + frames.length * 16);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(frames.length, 4);
  let offset = header.length;
  frames.forEach((frame, index) => {
    const entry = 6 + index * 16;
    header[entry] = header[entry + 1] = sizes[index] === 256 ? 0 : sizes[index];
    header.writeUInt16LE(1, entry + 4);
    header.writeUInt16LE(32, entry + 6);
    header.writeUInt32LE(frame.length, entry + 8);
    header.writeUInt32LE(offset, entry + 12);
    offset += frame.length;
  });
  await writeFile(path(name), Buffer.concat([header, ...frames]));
}

/** `content` resized to `size` and centred on a transparent square of `canvas` pixels. */
async function padded(content: Buffer, size: number, canvas: number) {
  const inset = Math.round((canvas - size) / 2);
  return sharp(await sharp(content).resize(size, size).png().toBuffer())
    .extend({
      top: inset,
      bottom: canvas - size - inset,
      left: inset,
      right: canvas - size - inset,
      background: "transparent",
    })
    .png()
    .toBuffer();
}

for (const app of ["web", "www"]) {
  const publicDir = `apps/${app}/public`;
  await png(badge, `${publicDir}/favicon-16x16.png`, 16);
  await png(badge, `${publicDir}/favicon-32x32.png`, 32);
  await png(badge, `${publicDir}/favicon.png`, 128);
  await png(square, `${publicDir}/apple-touch-icon.png`, 180);
  await png(square, `${publicDir}/icon-192.png`, 192);
  await png(square, `${publicDir}/icon-512.png`, 512);
  await ico(badge, `${publicDir}/favicon.ico`, [16, 32, 48]);
}
await mkdir(path("apps/www/public/brand"), { recursive: true });
await png(badge, "apps/www/public/brand/rakazo-mark.png", 256);
await png(badge, "apps/mobile/assets/favicon.png", 128);
await png(square, "apps/desktop/assets/icon.png", 1024);
await ico(rounded, "apps/desktop/assets/icon.ico", [16, 32, 48, 64, 128, 256]);
await sharp(square).flatten().png().toFile(path("apps/mobile/assets/icon.png"));

// Icon Composer reads the layer by name; Electron and Expo compile it into the Apple icons.
await png(square, "packages/ui-tokens/assets/Rakazo.icon/Assets/orange-bot.png", 1024);

// Pre-Tahoe macOS development icon: the macOS grid's 824-point plate inside Dock margins.
const plate = await masked(square, '<rect width="1024" height="1024" rx="230" fill="white"/>');
await sharp(await padded(plate, 824, 1024)).toFile(path("apps/desktop/assets/icon-macos.png"));

// Android supplies the mask; keep the ring inside the launcher's safe circle.
await sharp({
  create: { width: 1024, height: 1024, channels: 3, background: darkTokens.background },
})
  .png()
  .toFile(path("apps/mobile/assets/icon-background.png"));
await sharp(await padded(badge, 700, 1024)).toFile(path("apps/mobile/assets/adaptive-icon.png"));

// The splash shows the badge alone, so it sits on either splash colour.
await sharp(await padded(badge, 620, 1024)).toFile(path("apps/mobile/assets/splash-icon.png"));

// Themed launchers and notifications need a white alpha silhouette: the ring and bull are
// the bright strokes on the navy plate, so brightness above the plate becomes alpha.
const { data, info } = await sharp(badge).raw().toBuffer({ resolveWithObject: true });
const alpha = Buffer.alloc(info.width * info.height);
for (let pixel = 0; pixel < alpha.length; pixel++) {
  const offset = pixel * info.channels;
  const bright = Math.max(data[offset], data[offset + 1], data[offset + 2]);
  const edge = data[offset + 3] / 255;
  alpha[pixel] = Math.round(Math.min(1, Math.max(0, (bright - 70) / 80)) * edge * 255);
}
const silhouette = await sharp({
  create: { width: info.width, height: info.height, channels: 3, background: "white" },
})
  .joinChannel(alpha, { raw: { width: info.width, height: info.height, channels: 1 } })
  .png()
  .toBuffer();
await sharp(await padded(silhouette, 700, 1024)).toFile(
  path("apps/mobile/assets/monochrome-icon.png"),
);
await png(silhouette, "apps/mobile/assets/notification-icon.png", 72);

// Social cards: the badge centred on the brand navy.
const card = await sharp({
  create: { width: 1200, height: 630, channels: 3, background: darkTokens.background },
})
  .composite([{ input: await sharp(badge).resize(460, 460).png().toBuffer(), top: 85, left: 370 }])
  .png()
  .toBuffer();
await sharp(card).ensureAlpha().png().toFile(path("apps/www/public/og-image.png"));
for (const file of await readdir(path("apps/www/public/og"))) {
  if (file.endsWith(".png")) {
    await sharp(card)
      .removeAlpha()
      .png()
      .toFile(path(`apps/www/public/og/${file}`));
  }
}
