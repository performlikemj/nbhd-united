import { copyFile, mkdir, readFile } from "node:fs/promises";

const source = new URL("../public/.well-known/apple-app-site-association", import.meta.url);
const destination = new URL("../out/.well-known/", import.meta.url);

// Guarantee the extensionless handshake survives static export, including
// Next versions that omit dot-directories when copying public assets.
JSON.parse(await readFile(source, "utf8"));
await mkdir(destination, { recursive: true });
await copyFile(source, new URL("apple-app-site-association", destination));
