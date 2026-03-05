const sharp = require("sharp");
const fs = require("fs");
const path = require("path");

const PUBLIC = path.join(__dirname, "..", "public");
const CREAM = { r: 248, g: 246, b: 239, alpha: 1 }; // #f8f6ef
const TRANSPARENT = { r: 0, g: 0, b: 0, alpha: 0 };

const dirs = [
  path.join(PUBLIC, "icons", "favicon"),
  path.join(PUBLIC, "icons", "pwa"),
  path.join(PUBLIC, "icons", "brand"),
];

async function makeSquare(inputPath, padding = 0.1) {
  // Trim whitespace/transparency, then re-encode as PNG to get a clean buffer
  const trimmedBuf = await sharp(inputPath).trim().png().toBuffer();
  const { width, height } = await sharp(trimmedBuf).metadata();

  const side = Math.round(Math.max(width, height) * (1 + padding * 2));
  const padX = Math.round((side - width) / 2);
  const padY = Math.round((side - height) / 2);

  return sharp(trimmedBuf)
    .extend({
      top: padY,
      bottom: padY,
      left: padX,
      right: padX,
      background: TRANSPARENT,
    })
    .png()
    .toBuffer();
}

async function generate(squareBuffer, size, outputPath, opts = {}) {
  const { background } = opts;
  let pipeline = sharp(squareBuffer).resize(size, size, {
    fit: "contain",
    background: background || TRANSPARENT,
  });

  if (background) {
    pipeline = pipeline.flatten({ background });
  }

  await pipeline.png().toFile(outputPath);
  console.log(`  ${path.relative(PUBLIC, outputPath)} (${size}x${size})`);
}

async function main() {
  for (const dir of dirs) {
    fs.mkdirSync(dir, { recursive: true });
  }

  const whiteSrc = path.join(PUBLIC, "images", "icon-white.png");
  const greenSrc = path.join(PUBLIC, "images", "logo-light.png");

  if (!fs.existsSync(whiteSrc) || !fs.existsSync(greenSrc)) {
    console.error("Source images not found. Ensure icon-white.png and logo-light.png exist in public/images/");
    process.exit(1);
  }

  // --- White line-art (transparent bg) ---
  console.log("Processing white line-art icon...");
  const whiteSquare = await makeSquare(whiteSrc, 0.12);

  await generate(whiteSquare, 16, path.join(PUBLIC, "icons", "favicon", "favicon-16x16.png"));
  await generate(whiteSquare, 32, path.join(PUBLIC, "icons", "favicon", "favicon-32x32.png"));
  await generate(whiteSquare, 64, path.join(PUBLIC, "icons", "brand", "icon-white-64.png"));

  // Copy 32x32 PNG as favicon.ico (modern browsers handle PNG favicons)
  fs.copyFileSync(
    path.join(PUBLIC, "icons", "favicon", "favicon-32x32.png"),
    path.join(PUBLIC, "favicon.ico")
  );
  console.log("  favicon.ico");

  // --- Green filled (for apple/pwa/brand) ---
  console.log("Processing green filled icon...");
  const greenSquare = await makeSquare(greenSrc, 0.08);

  await generate(greenSquare, 64, path.join(PUBLIC, "icons", "brand", "icon-green-64.png"));
  await generate(greenSquare, 180, path.join(PUBLIC, "icons", "apple-touch-icon.png"), { background: CREAM });
  await generate(greenSquare, 192, path.join(PUBLIC, "icons", "pwa", "icon-192.png"), { background: CREAM });
  await generate(greenSquare, 512, path.join(PUBLIC, "icons", "pwa", "icon-512.png"), { background: CREAM });

  // Maskable icon — icon in inner 60%, cream background
  console.log("Processing maskable icon...");
  const maskableInner = Math.round(512 * 0.6);
  const maskablePad = Math.round((512 - maskableInner) / 2);
  await sharp(greenSquare)
    .resize(maskableInner, maskableInner, { fit: "contain", background: CREAM })
    .flatten({ background: CREAM })
    .extend({
      top: maskablePad,
      bottom: maskablePad,
      left: maskablePad,
      right: maskablePad,
      background: CREAM,
    })
    .resize(512, 512)
    .png()
    .toFile(path.join(PUBLIC, "icons", "pwa", "maskable-512.png"));
  console.log("  icons/pwa/maskable-512.png (512x512, maskable)");

  console.log("\nDone! All icons generated.");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
