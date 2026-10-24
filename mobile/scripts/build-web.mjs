// Copies the virtual-phone UI into www/ for the Android app. Same files, so the app and the
// virtual phone can never drift apart. Absolute /static/ paths become relative for the app.
import { cpSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
const src = new URL("../../jarvis/server/static/", import.meta.url).pathname;
const out = new URL("../www/", import.meta.url).pathname;
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
cpSync(src, out, { recursive: true });
for (const f of ["index.html", "phone.js"]) {
  const p = out + f;
  writeFileSync(p, readFileSync(p, "utf8").replaceAll("/static/", "./"));
}
// In the app, the page is served from https://localhost; add a CSP meta (the server sends a header instead).
const idx = out + "index.html";
writeFileSync(idx, readFileSync(idx, "utf8").replace("<head>",
  `<head>\n<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src https: wss:; img-src 'self' data:; media-src 'self' blob:">`));
console.log("www/ built from", src);
