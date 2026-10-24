// JARVIS phone client. Runs in the browser (virtual phone) and inside the Android app.
// In the app, window.Capacitor.Plugins.Jarvis provides native wake word, STT, TTS, biometrics
// and device actions; in the browser the same calls fall back to Web APIs and a simulated phone.

const $ = (id) => document.getElementById(id);
const Native = window.Capacitor?.Plugins?.Jarvis || null;
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};
const IN_APP = !!Native;
let SERVER = store.get("jarvis_server") || (IN_APP ? "" : location.origin);
let token = null;
// The pairing token lives in Android Keystore-backed storage in the app, localStorage in the browser.
const secret = {
  async get() { if (Native?.secureGet) return (await Native.secureGet({ key: "jarvis_token" })).value; return store.get("jarvis_token"); },
  async set(v) { if (Native?.secureSet) return Native.secureSet({ key: "jarvis_token", value: v }); store.set("jarvis_token", v); },
  async del() { if (Native?.secureSet) return Native.secureSet({ key: "jarvis_token", value: null }); store.del("jarvis_token"); },
};
let ws = null, wsReady = false, retry = 0;

// ------------------------------------------------------------------ orb (the one bold element)
const orb = (() => {
  const c = $("orb"), ctx = c.getContext("2d");
  const W = c.width, H = c.height, cx = W / 2, cy = H / 2;
  let mode = "idle", level = 0, t = 0;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const rings = Array.from({ length: 7 }, (_, i) => ({
    r: 70 + i * 26, w: 1 + (i % 3), segs: 3 + (i * 5) % 9, speed: (i % 2 ? 1 : -1) * (0.15 + i * 0.05), phase: i * 1.7,
  }));
  const sparks = Array.from({ length: 140 }, () => ({ a: Math.random() * 6.283, r: 40 + Math.random() * 220, s: 0.2 + Math.random() }));
  const speed = { idle: 0.4, listening: 1.6, thinking: 2.6, speaking: 1.2, offline: 0.1 };
  function frame() {
    t += 0.016 * (speed[mode] ?? 1);
    ctx.clearRect(0, 0, W, H);
    const pulse = mode === "speaking" ? 0.5 + 0.5 * Math.sin(t * 9) : mode === "listening" ? 0.3 + level : 0.15;
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, 120 + pulse * 30);
    g.addColorStop(0, `rgba(255,217,138,${0.55 + pulse * 0.35})`);
    g.addColorStop(0.35, "rgba(242,164,58,0.25)");
    g.addColorStop(1, "rgba(242,164,58,0)");
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, 160, 0, 6.283); ctx.fill();
    for (const ring of rings) {
      const seg = 6.283 / ring.segs;
      ctx.lineWidth = ring.w;
      for (let k = 0; k < ring.segs; k++) {
        const a0 = ring.phase + t * ring.speed + k * seg;
        const len = seg * (0.35 + 0.3 * Math.sin(k * 3.1 + t * 0.7));
        ctx.strokeStyle = `rgba(242,164,58,${mode === "offline" ? 0.15 : 0.35 + 0.4 * ((k + ring.segs) % 3) / 3})`;
        ctx.beginPath(); ctx.arc(cx, cy, ring.r * (1 + pulse * 0.04), a0, a0 + len); ctx.stroke();
      }
    }
    ctx.fillStyle = "rgba(255,217,138,0.8)";
    for (const p of sparks) {
      const a = p.a + t * 0.2 * p.s * (mode === "thinking" ? 3 : 1);
      const r = p.r + Math.sin(t * p.s + p.a) * 6;
      ctx.globalAlpha = 0.2 + 0.6 * p.s * (mode === "offline" ? 0.2 : 1);
      ctx.fillRect(cx + Math.cos(a) * r, cy + Math.sin(a) * r * 0.92, 1.6, 1.6);
    }
    ctx.globalAlpha = 1;
    if (!reduce) requestAnimationFrame(frame);
  }
  frame();
  return { set(m) { mode = m; if (reduce) frame(); }, level(v) { level = Math.min(1, v); } };
})();

const STATE_TEXT = { idle: "Ready", listening: "Listening", thinking: "Thinking", speaking: "Speaking", offline: "Reconnecting" };
function setState(m) { orb.set(m); $("state").textContent = STATE_TEXT[m] || m; }

// ------------------------------------------------------------------ simulated phone (browser only)
const phone = {
  alarms: [], timers: [], reminders: [], sms: [],
  settings: { wifi: true, bluetooth: false, flashlight: false, dnd: false, silent: false },
  battery: 78,
  location: { lat: 12.9916, lon: 80.2336, name: "IIT Madras" },
  contacts: [
    { name: "Arun Kumar", phone: "+91 90000 11111", email: "arun.demo@example.com" },
    { name: "Priya S", phone: "+91 90000 22222", email: "priya.demo@example.com" },
    { name: "Prof. Ravi", phone: "+91 90000 33333", email: "ravi.demo@example.com" },
    { name: "Mom", phone: "+91 90000 44444", email: "" },
    { name: "Kavya", phone: "+91 90000 55555", email: "kavya.demo@example.com" },
  ],
  notifications: [
    { app: "WhatsApp", title: "Arun", text: "Lab meeting moved to 4?", time: "21:50" },
    { app: "HDFC Bank", title: "Debit alert", text: "Rs 450 debited from a/c XX1234", time: "20:10" },
    { app: "Messages", title: "AX-SWIGGY", text: "Your OTP is 482913. Do not share.", time: "19:02" },
    { app: "Gmail", title: "Prof. Ravi", text: "Thesis draft comments", time: "18:30" },
  ],
  apps: ["Spotify", "WhatsApp", "Gmail", "Maps", "Camera", "Chrome", "Slack", "GitHub"],
};
const BLOCKED_APPS = /g ?pay|phone ?pe|paytm|bhim|bank|hdfc|icici|sbi|axis|kotak|password|authenticator|wallet|aadhaar/i;

async function runDevice(command, args) {
  if (Native?.device) return Native.device({ command, args });
  switch (command) {
    case "set_alarm": phone.alarms.push({ ...args }); renderClock(); toast(`Alarm set ${pad(args.hour)}:${pad(args.minute)}`); return { ok: true };
    case "set_timer": {
      const end = Date.now() + args.seconds * 1000; phone.timers.push({ ...args, end }); renderClock();
      setTimeout(() => { toast(`${args.label || "Timer"} done`); speak(`${args.label || "Your timer"} is done.`); }, Math.min(args.seconds * 1000, 2 ** 31 - 1));
      return { ok: true };
    }
    case "set_reminder": {
      phone.reminders.push({ ...args }); renderClock();
      const ms = new Date(args.at_iso).getTime() - Date.now();
      if (ms > 0 && ms < 2 ** 31) setTimeout(() => { toast(`Reminder: ${args.label}`); speak(`Reminder: ${args.label}.`); }, ms);
      return { ok: true };
    }
    case "open_app": {
      if (BLOCKED_APPS.test(args.app)) return { ok: false, error: "blocked app" };
      const app = phone.apps.find((a) => a.toLowerCase().includes(String(args.app).toLowerCase()));
      if (!app) return { ok: false, error: `${args.app} is not installed` };
      toast(`Opening ${app}`); return { ok: true, app };
    }
    case "call": $("dial-name").textContent = args.name || args.number; $("dial-num").textContent = args.number; $("dialer").classList.remove("hidden"); return { ok: true };
    case "send_sms": phone.sms.push({ ...args }); renderClock(); toast(`Text sent to ${args.name || args.number}`); return { ok: true };
    case "find_contact": {
      const q = String(args.name || "").toLowerCase();
      return { ok: true, contacts: phone.contacts.filter((c) => c.name.toLowerCase().includes(q)) };
    }
    case "get_location": return { ok: true, ...phone.location };
    case "read_notifications": // filtered here AND on the server
      return { ok: true, notifications: phone.notifications.filter((n) => !BLOCKED_APPS.test(n.app) && !/\b(otp|code)\b/i.test(n.text)) };
    case "toggle":
      if (!(args.setting in phone.settings)) return { ok: false, error: "unsupported" };
      phone.settings[args.setting] = !!args.state; renderStatus(); return { ok: true, setting: args.setting, state: !!args.state };
    case "battery": {
      try { const b = await navigator.getBattery(); phone.battery = Math.round(b.level * 100); return { ok: true, level: phone.battery, charging: b.charging }; }
      catch { return { ok: true, level: phone.battery, charging: false }; }
    }
  }
  return { ok: false, error: "unknown command" };
}
$("dial-end").onclick = () => $("dialer").classList.add("hidden");

// ------------------------------------------------------------------ speech out (free: Android TTS / speechSynthesis)
let voice = null;
function pickVoice() {
  const vs = speechSynthesis?.getVoices?.() || [];
  voice = vs.find((v) => /en-GB/i.test(v.lang) && /male|daniel|george|arthur/i.test(v.name)) || vs.find((v) => /en-GB/i.test(v.lang)) || vs.find((v) => /^en/i.test(v.lang)) || null;
}
if ("speechSynthesis" in window) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }
let muted = false;
async function speak(text) {
  if (muted || !text) return;
  const clean = text.replace(/https?:\/\/\S+/g, "the link").replace(/\S+@\S+\.\S+/g, (m) => m.split("@")[0]);
  setState("speaking");
  try {
    if (Native?.speak) { await Native.speak({ text: clean }); return; }
    if (!("speechSynthesis" in window)) return;
    await new Promise((res) => {
      const u = new SpeechSynthesisUtterance(clean);
      if (voice) u.voice = voice;
      u.rate = 1.03; u.pitch = 0.95; u.onend = res; u.onerror = res;
      speechSynthesis.cancel(); speechSynthesis.speak(u);
      setTimeout(res, 20000);
    });
  } finally { setState("idle"); }
}

// ------------------------------------------------------------------ speech in (free: Android SpeechRecognizer / Web Speech API)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null, listening = false;
// Speech engine: "whisper" (server, Groq Whisper; much better with Indian accents) or "phone".
let sttMode = store.get("jarvis_stt") || "whisper";
let serverStt = false;  // set from /api/status at start
let stopRecording = null;

async function listen() {
  if (listening) return;
  speechSynthesis?.cancel?.();
  if (sttMode === "whisper" && serverStt) return recordForServer();
  if (Native?.listen) {
    listening = true; setState("listening"); $("mic").classList.add("live");
    let r = null, failed = null;
    try { r = await Native.listen({ language: "en-IN" }); }
    catch (e) { failed = String(e?.message || e); }
    finally { listening = false; $("mic").classList.remove("live"); setState("idle"); }
    if (r?.text) { send(r.text, "voice"); return; }
    Native.resumeWake?.();
    const err = failed || r?.error;
    if (!err || err === "no_match" || err === "timeout") { note("I didn't catch that. Tap the mic and try again."); return; }
    if (/permission/i.test(err)) { note("JARVIS needs the microphone. Allow it in Settings > Apps > JARVIS > Permissions."); return; }
    // Phone recognizer missing or broken (common on some Samsung phones): record here, transcribe on the server.
    note(`Phone speech recognition failed (${err}); using server transcription instead.`);
    return recordForServer();
  }
  if (!SR) return recordForServer();
  rec = new SR();
  rec.lang = "en-IN"; rec.interimResults = true; rec.maxAlternatives = 1;
  let finalText = "";
  rec.onresult = (e) => {
    let interim = "";
    for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim = r[0].transcript));
    $("text").value = finalText || interim;
  };
  rec.onend = () => { listening = false; $("mic").classList.remove("live"); setState("idle"); if (finalText.trim()) { $("text").value = ""; send(finalText, "voice"); } };
  rec.onerror = () => {};
  listening = true; $("mic").classList.add("live"); setState("listening");
  rec.start();
  // Some browsers never fire onend if recognition stalls; never leave the mic stuck "on".
  setTimeout(() => { if (listening && rec) { try { rec.abort(); } catch {} listening = false; $("mic").classList.remove("live"); setState("idle"); } }, 15000);
}
async function recordForServer() {
  // Records until you stop talking (or tap the mic), then transcribes on your server with Whisper.
  await Native?.pauseWake?.();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
  catch { note("Microphone access was blocked. Allow it for JARVIS and try again."); Native?.resumeWake?.(); return; }
  const actx = new AudioContext();
  const an = actx.createAnalyser(); an.fftSize = 1024;
  actx.createMediaStreamSource(stream).connect(an);
  const buf = new Float32Array(an.fftSize);
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => window.MediaRecorder?.isTypeSupported?.(t)) || "";
  const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  listening = true; setState("listening"); $("mic").classList.add("live");
  let heard = false, noise = 0.01;   // running estimate of background level
  const t0 = performance.now(); let lastLoud = t0;
  const finish = () => { if (mr.state === "recording") mr.stop(); };
  stopRecording = finish;
  const timer = setInterval(() => {
    an.getFloatTimeDomainData(buf);
    let sum = 0; for (const v of buf) sum += v * v;
    const rms = Math.sqrt(sum / buf.length), now = performance.now();
    orb.level(rms * 10);
    // Background level follows the quietest moments (fast down, slow up), so starting to speak
    // immediately doesn't get mistaken for noise.
    noise = rms < noise ? rms : noise * 0.995 + rms * 0.005;
    if (rms > Math.max(0.012, noise * 3)) { heard = true; lastLoud = now; }
    if ((heard && now - lastLoud > 1300) || now - t0 > 15000 || (!heard && now - t0 > 7000)) finish();
  }, 50);
  mr.start(250);
  mr.onstop = async () => {
    clearInterval(timer); stopRecording = null;
    stream.getTracks().forEach((t) => t.stop()); actx.close();
    listening = false; $("mic").classList.remove("live");
    if (!heard) { setState("idle"); Native?.resumeWake?.(); note("I didn't hear anything. Tap the mic and speak."); return; }
    setState("thinking");
    const r = await api("/api/stt", { method: "POST", body: new Blob(chunks, { type: mr.mimeType || "audio/webm" }), raw: true }).catch(() => null);
    setState("idle"); Native?.resumeWake?.();
    if (r?.text?.trim()) send(r.text, "voice");
    else note("Transcription failed. Check the server's Groq key, or switch voice input to the phone in Settings.");
  };
}
$("mic").onclick = () => {
  if (listening && stopRecording) return stopRecording();   // tap again to finish early
  if (listening && rec) return rec.stop();
  listen();
};

// ------------------------------------------------------------------ "Hey JARVIS" wake word
let wakeCtx = null, wakeStream = null;
async function setWake(on) {
  $("wake-toggle").setAttribute("aria-pressed", on);
  $("wake-label").textContent = on ? "Listening for Hey JARVIS" : "Hey JARVIS is off";
  store.set("jarvis_wake", on ? "1" : "0");
  if (Native?.wakeWord) { await Native.wakeWord({ enabled: on }); return; }
  if (!on) { wakeStream?.getTracks().forEach((t) => t.stop()); await wakeCtx?.close(); wakeCtx = null; return; }
  try {
    wakeStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    wakeCtx = new AudioContext();
    await wakeCtx.audioWorklet.addModule("/static/pcm-worklet.js");
    const src = wakeCtx.createMediaStreamSource(wakeStream);
    const node = new AudioWorkletNode(wakeCtx, "pcm-worklet");
    node.port.onmessage = (e) => {
      if (wsReady && !listening) ws.send(e.data);
      const a = new Int16Array(e.data); let s = 0; for (let i = 0; i < a.length; i += 8) s += Math.abs(a[i]);
      if (listening) orb.level(s / (a.length / 8) / 6000);
    };
    src.connect(node);
  } catch (err) {
    note("Microphone access was blocked, so Hey JARVIS can't listen. Allow the microphone in the browser and try again.");
    $("wake-toggle").setAttribute("aria-pressed", "false");
  }
}
$("wake-toggle").onclick = () => setWake($("wake-toggle").getAttribute("aria-pressed") !== "true");
if (Native?.addListener) Native.addListener("wake", () => onWake());
function chime() {
  try {
    const a = new AudioContext(), o = a.createOscillator(), g = a.createGain();
    o.frequency.setValueAtTime(660, a.currentTime); o.frequency.exponentialRampToValueAtTime(990, a.currentTime + 0.12);
    g.gain.setValueAtTime(0.12, a.currentTime); g.gain.exponentialRampToValueAtTime(0.001, a.currentTime + 0.25);
    o.connect(g).connect(a.destination); o.start(); o.stop(a.currentTime + 0.26);
  } catch {}
}
function onWake() { window.__wakes = (window.__wakes || 0) + 1; chime(); listen(); }

// ------------------------------------------------------------------ connection
async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body && !raw) headers["Content-Type"] = "application/json";
  const r = await fetch(SERVER + path, { method, headers, body: raw ? body : body && JSON.stringify(body) });
  if (r.status === 401) { unpair("The pairing token was rejected. Pair again."); return null; }
  return r.ok ? r.json() : null;
}

function connect() {
  const url = SERVER.replace(/^http/, "ws") + "/ws";
  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token, device: true }));
  ws.onmessage = async (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") { wsReady = true; retry = 0; setState("idle"); showRequests(m.pending || []); if (store.get("jarvis_wake") === "1") setWake(true); }
    else if (m.type === "error") unpair("The pairing token was rejected. Pair again.");
    else if (m.type === "reply") onReply(m);
    else if (m.type === "device_command") ws.send(JSON.stringify({ type: "device_result", id: m.id, result: await runDevice(m.command, m.args) }));
    else if (m.type === "wake") onWake();
    else if (m.type === "notify") { toast(m.text); if (m.speak) speak(m.text); }
  };
  ws.onclose = (e) => {
    wsReady = false;
    if (e.code === 4401) return;
    setState("offline");
    setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
  };
}

function unpair(msg) {
  secret.del(); token = null; ws?.close();
  $("main").classList.add("hidden"); $("tabs").classList.add("hidden"); $("pair").classList.remove("hidden");
  $("pair-err").textContent = msg || "";
}

$("pair-form").onsubmit = async (e) => {
  e.preventDefault();
  token = $("pair-token").value.trim();
  const srv = $("pair-server").value.trim().replace(/\/+$/, "");
  if (srv) {
    if (!/^https:\/\//.test(srv) && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(srv)) { $("pair-err").textContent = "Use an https:// address so the connection is encrypted."; return; }
    SERVER = srv;
  }
  if (!SERVER) { $("pair-err").textContent = "Enter the server address."; return; }
  let ok;
  try { ok = await fetch(SERVER + "/api/status", { headers: { Authorization: `Bearer ${token}` } }); }
  catch { $("pair-err").textContent = "Can't reach the server. Check the address and your connection."; return; }
  if (!ok.ok) { $("pair-err").textContent = ok.status === 401 ? "That token doesn't match this server." : "Can't reach the server."; return; }
  store.set("jarvis_server", SERVER); await secret.set(token); start();
};

// ------------------------------------------------------------------ conversation
function send(text, source = "text") {
  text = text.trim(); if (!text) return;
  addLine("you", text);
  setState("thinking");
  if (wsReady) ws.send(JSON.stringify({ type: "message", text, source }));
  else api("/api/message", { method: "POST", body: { text, source } }).then((r) => r && onReply(r));
}
$("composer").onsubmit = (e) => { e.preventDefault(); const t = $("text").value; $("text").value = ""; send(t); };

function onReply(m) {
  setState("idle");
  const agents = [...new Set(m.agents || [])].filter((a) => a !== "router").join(" + ");
  addLine("jarvis", m.reply, agents, m.cards || []);
  speak(m.reply);
  showRequests(m.pending || []);
}

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function addLine(who, text, agent = "", cards = []) {
  const li = el("li", who);
  if (who === "jarvis" && agent) li.append(el("span", "who", agent));
  li.append(el("span", "", text));
  for (const c of cards) { const n = renderCard(c); if (n) li.append(n); }
  $("log").append(li);
  $("log").scrollTop = $("log").scrollHeight;
}
function note(text) { addLine("note", text); }
const fmtT = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const fmtD = (iso) => new Date(iso).toLocaleDateString([], { weekday: "short" });

function renderCard(c) {
  if (c.type === "events" && c.events.length) {
    const box = el("div", "card");
    for (const e of c.events.slice(0, 6)) {
      const r = el("div", "r"); r.append(el("span", "t", fmtT(e.start)));
      const d = el("span", "", e.title); if (e.location) d.append(el("span", "s", `  ${e.location}`));
      r.append(d); box.append(r);
    }
    return box;
  }
  if (c.type === "slots") {
    const box = el("div", "slots");
    c.slots.forEach(([s], i) => { const b = el("button", "", `${fmtD(s)} ${fmtT(s)}`); b.onclick = () => send(["book the first one", "book the second one", "book the third one"][i]); box.append(b); });
    return box;
  }
  if (c.type === "emails") {
    const box = el("div", "card");
    for (const e of c.emails.slice(0, 5)) {
      const r = el("div", "r"); r.append(el("span", "t", e.unread ? "New" : ""));
      const d = el("span", "", `${e.from}: ${e.subject}`);
      if (e.suspicious) d.append(el("span", "flag", "  Looks like phishing. Ignored."));
      r.append(d); box.append(r);
    }
    return box;
  }
  if (c.type === "notifications") {
    const box = el("div", "card");
    for (const n of c.items) { const r = el("div", "r"); r.append(el("span", "t", n.app)); r.append(el("span", "", `${n.title}: ${n.text}`)); box.append(r); }
    return box;
  }
  if (c.type === "plan") {
    const box = el("div", "card");
    c.steps.forEach((s, i) => { const r = el("div", "r"); r.append(el("span", "t", `Step ${i + 1}`)); r.append(el("span", "", s.request)); box.append(r); });
    return box;
  }
  return null;
}

// ------------------------------------------------------------------ permission prompts and approvals
let queue = [], showing = null;
function showRequests(pending) {
  queue = pending;
  $("badge").textContent = pending.length; $("badge").classList.toggle("hidden", !pending.length);
  if (showing && pending.some((p) => p.id === showing.id)) return;
  showing = null; $("perm").classList.add("hidden"); $("appr").classList.add("hidden");
  const next = pending.find((p) => p.kind === "permission") || pending[0];
  if (!next) return;
  showing = next;
  if (next.kind === "permission") {
    $("perm-title").textContent = `Allow JARVIS to ${next.title}?`;
    $("perm-why").textContent = next.why;
    $("perm").classList.remove("hidden");
    $("perm").querySelector(".primary").focus();
  } else openApproval(next);
}
$("perm").querySelectorAll("button[data-mode]").forEach((b) => (b.onclick = () => {
  const id = showing.id; showing = null; $("perm").classList.add("hidden"); setState("thinking");
  decide({ type: "permission", id, mode: b.dataset.mode }, `/api/permission/${id}`, { mode: b.dataset.mode });
}));

function decide(wsMsg, path, body) {
  if (wsReady) ws.send(JSON.stringify(wsMsg));
  else api(path, { method: "POST", body }).then((r) => r && onReply(r));
}

let editing = false;
function openApproval(r) {
  editing = false;
  $("appr-title").textContent = r.strong_auth ? "Confirm on screen before JARVIS acts" : "Approve before JARVIS acts";
  $("appr-reason").textContent = r.reason || "";
  $("appr-preview").textContent = r.preview;
  $("appr-preview").classList.remove("hidden");
  $("appr-edit").classList.add("hidden"); $("appr-edit").replaceChildren();
  $("appr-editbtn").classList.toggle("hidden", !(r.editable || []).length);
  $("appr-yes-label").textContent = r.strong_auth ? "Hold to confirm" : "Approve";
  $("appr").classList.remove("hidden");
}
$("appr-editbtn").onclick = () => {
  const r = showing; editing = true;
  $("appr-preview").classList.add("hidden");
  const box = $("appr-edit"); box.replaceChildren(); box.classList.remove("hidden");
  for (const f of r.editable) {
    const lab = el("label", "", f[0].toUpperCase() + f.slice(1)); lab.htmlFor = `edit-${f}`;
    const inp = f === "body" || f === "text" ? el("textarea") : el("input");
    inp.id = `edit-${f}`; inp.value = r.args[f] ?? ""; box.append(lab, inp);
  }
};
function finishApproval(approved, via) {
  const r = showing; if (!r) return;
  const edits = {};
  if (editing) for (const f of r.editable) edits[f] = $(`edit-${f}`).value;
  showing = null; $("appr").classList.add("hidden"); setState("thinking");
  decide({ type: "approval", id: r.id, approved, edits, via }, `/api/approval/${r.id}`, { approved, edits, via });
}
$("appr-no").onclick = () => finishApproval(false, "tap");
// Strong confirmation: press and hold (on the phone this is the system biometric prompt).
let holdT = null;
const yes = $("appr-yes");
yes.addEventListener("pointerdown", async () => {
  if (!showing?.strong_auth) return;
  if (Native?.biometric) { const ok = await Native.biometric({ reason: "Confirm this action" }).catch(() => null); if (ok?.verified) finishApproval(true, "biometric"); return; }
  yes.classList.add("hold"); holdT = setTimeout(() => { yes.classList.remove("hold"); finishApproval(true, "biometric"); }, 1200);
});
["pointerup", "pointerleave"].forEach((ev) => yes.addEventListener(ev, () => { clearTimeout(holdT); yes.classList.remove("hold"); }));
yes.onclick = () => { if (showing && !showing.strong_auth) finishApproval(true, "tap"); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("appr").classList.contains("hidden")) finishApproval(false, "tap"); });

// ------------------------------------------------------------------ panels
document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  for (const v of ["main", "clockapp", "security"]) $(v).classList.toggle("hidden", v !== b.dataset.view);
  if (b.dataset.view === "security") loadSecurity();
}));
const pad = (n) => String(n).padStart(2, "0");
function rows(id, items, fmt) {
  const ul = $(id); ul.replaceChildren();
  if (!items.length) { ul.append(el("li", "empty", "Nothing yet")); return; }
  for (const it of items) ul.append(fmt(it));
}
function renderClock() {
  rows("alarms", phone.alarms, (a) => { const li = el("li", "", `${pad(a.hour)}:${pad(a.minute)}`); li.append(el("span", "mode", a.label)); return li; });
  rows("timers", phone.timers, (t) => { const li = el("li", "", `${Math.round(t.seconds / 60)} min`); li.append(el("span", "mode", t.label)); return li; });
  rows("reminders", phone.reminders, (r) => { const li = el("li", "", r.label); li.append(el("span", "mode", new Date(r.at_iso).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }))); return li; });
  rows("smslog", phone.sms, (s) => { const li = el("li", "", s.text); li.append(el("span", "mode", s.name || s.number)); return li; });
}
function renderVoiceChoice() {
  document.querySelectorAll("input[name=stt]").forEach((r) => {
    r.checked = r.value === sttMode;
    r.onchange = () => { sttMode = r.value; store.set("jarvis_stt", sttMode); };
  });
  $("stt-note").textContent = serverStt ? "" : "Whisper needs a Groq key on the server; until then the phone recognizer is used.";
}
async function loadSecurity() {
  renderVoiceChoice();
  const p = await api("/api/permissions"); const a = await api("/api/audit");
  if (!p) return;
  $("unlock").classList.toggle("hidden", !p.locked);
  rows("perms", p.permissions, (x) => {
    const li = el("li"); const t = el("span", "", x.title[0].toUpperCase() + x.title.slice(1)); t.append(el("span", "sub", x.why)); li.append(t);
    if (x.mode === "always" || x.mode === "once") { const b = el("button", "revoke", "Revoke"); b.onclick = async () => { await api("/api/permissions/revoke", { method: "POST", body: { scope: x.scope } }); loadSecurity(); }; li.append(b); }
    else li.append(el("span", `mode ${x.mode}`, x.mode === "deny" ? "Denied" : "Not asked"));
    return li;
  });
  rows("audit", (a?.audit || []).slice(0, 30), (x) => {
    const li = el("li", "", `${x.tool}`); li.append(el("span", "mode", `${x.outcome}, ${new Date(x.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`)); return li;
  });
}
$("lockdown").onclick = async () => { const r = await api("/api/lockdown", { method: "POST" }); if (r) { toast(`Locked down. ${r.cancelled} pending cancelled.`); loadSecurity(); showRequests([]); } };
$("unlock").onclick = async () => { if (prompt("Type UNLOCK to re-enable JARVIS") === "UNLOCK") { await api("/api/unlock", { method: "POST", body: { confirm: "UNLOCK" } }); loadSecurity(); } };

// ------------------------------------------------------------------ status bar + toast
function renderStatus() {
  $("sb-wifi").classList.toggle("off", !phone.settings.wifi);
  $("sb-bt").classList.toggle("hidden", !phone.settings.bluetooth);
  $("sb-dnd").classList.toggle("hidden", !phone.settings.dnd);
  $("sb-batt").style.setProperty("--lvl", `${phone.battery}%`); $("sb-batt").querySelector("b").textContent = phone.battery;
}
function tick() { $("clock").textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
setInterval(tick, 10000); tick();
let toastT = null;
function toast(text) { const t = $("toast"); t.textContent = text; t.classList.remove("hidden"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 4000); }

// ------------------------------------------------------------------ boot
function start() {
  $("pair").classList.add("hidden"); $("main").classList.remove("hidden"); $("tabs").classList.remove("hidden");
  setState("offline"); renderStatus(); renderClock();
  note("Say Hey JARVIS, tap the mic, or type.");
  api("/api/status").then((st) => { serverStt = !!st?.server_stt; }).catch(() => {});
  connect();
}
if (!IN_APP) $("server-field").classList.add("hidden");
if (Native?.addListener) Native.addListener("level", (e) => orb.level(e.level || 0));
secret.get().then((t) => { token = t; if (token && SERVER) start(); else $("tabs").classList.add("hidden"); });
window.__jarvis = { send, phone, setWake, listen, speakOff: () => (muted = true),
  setStt: (mode, available) => { sttMode = mode; serverStt = available; } }; // used by automated tests
