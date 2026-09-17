import { copyFile, mkdir, readFile } from "node:fs/promises";

const source = new URL("../public/.well-known/apple-app-site-association", import.meta.url);
const destination = new URL("../out/.well-known/", import.meta.url);

// Guarantee the handshake survives static export (Next omits dot-directories
// when copying public assets). Emit BOTH the extensionless file (Apple's
// required path, a valid 200 fallback) and a .json copy; the SWA route rewrites
// the extensionless path to the .json so Azure serves it as application/json.
JSON.parse(await readFile(source, "utf8"));
await mkdir(destination, { recursive: true });
await copyFile(source, new URL("apple-app-site-association", destination));
await copyFile(source, new URL("apple-app-site-association.json", destination));
