import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'export',
  images: {
    unoptimized: true,
  },
  // Pin the workspace root — Next 16 Turbopack (dev) otherwise mis-infers it
  // because this app lives inside the Django repo (no lockfile at the repo
  // root). The static `next build` (webpack) ignores this key.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
