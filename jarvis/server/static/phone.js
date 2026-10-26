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
  const vs = window.speechSynthesis?.getVoices?.() || [];
  const chosen = store.get("jarvis_voice");
  if (chosen && vs.find((v) => v.name === chosen)) { voice = vs.find((v) => v.name === chosen); return; }
  voice = vs.find((v) => /en-GB/i.test(v.lang) && /male|daniel|george|arthur/i.test(v.name)) || vs.find((v) => /en-GB/i.test(v.lang)) || vs.find((v) => /^en/i.test(v.lang)) || null;
}
if ("speechSynthesis" in window) { pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }
let muted = false;
async function speak(text) {
  if (muted || !text) return;
  const clean = text.replace(/https?:\/\/\S+/g, "the link").replace(/\S+@\S+\.\S+/g, (m) => m.split("@")[0]);
  setState("speaking");
  try {
    if (serverVoice && await speakServer(clean)) return;
    if (Native?.speak) { await Native.speak({ text: clean }).catch(() => {}); return; }
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

// The JARVIS server voice (Piper, British, with a synthetic treatment). Falls back to the phone's voice.
let serverVoice = store.get("jarvis_server_voice");     // null = never chosen: use the JARVIS voice if the server has it
let playing = null;
function ttsChunks(text) {
  const parts = text.match(/[^.!?]+[.!?]*\s*/g) || [text];
  const out = []; let cur = "";
  for (const p of parts) { if ((cur + p).length > 420 && cur) { out.push(cur); cur = ""; } cur += p; }
  if (cur.trim()) out.push(cur);
  return out.slice(0, 6);
}
async function fetchTts(text) {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 9000);
  try {
    const r = await fetch(SERVER + "/api/tts", { method: "POST", signal: ctl.signal,
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, body: JSON.stringify({ text, voice: serverVoice }) });
    return r.ok ? await r.blob() : null;
  } catch { return null; } finally { clearTimeout(t); }
}
async function speakServer(text) {
  const chunks = ttsChunks(text);
  let next = fetchTts(chunks[0]);
  for (let i = 0; i < chunks.length; i++) {
    const blob = await next;
    if (!blob) return i > 0;                      // first chunk failed: let the phone voice speak instead
    next = i + 1 < chunks.length ? fetchTts(chunks[i + 1]) : null;
    const url = URL.createObjectURL(blob);
    try {
      await new Promise((res) => {
        playing = new Audio(url); playing.onended = res; playing.onerror = res;
        playing.play().catch(res); setTimeout(res, 30000);
      });
    } finally { URL.revokeObjectURL(url); playing = null; }
    if (muted) break;
  }
  return true;
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
  window.speechSynthesis?.cancel?.();   // absent in Android's WebView: must go through window
  playing?.pause();
  if (sttMode === "whisper" && serverStt) return recordForServer();
  if (sttMode === "whisper" && !serverStt && !listen.warned) {
    listen.warned = true;
    note("Whisper isn't available on the server yet (update the server), so I'm using the phone recognizer.");
  }
  if (Native?.listen) {
    listening = true; setState("listening"); $("mic").classList.add("live");
    let r = null, failed = null;
    try {
      // Some recognizers never call back; give up after 20 s instead of leaving the mic stuck.
      r = await Promise.race([
        Native.listen({ language: "en-IN" }),
        new Promise((_, rej) => setTimeout(() => rej(new Error("no response from phone recognizer")), 20000)),
      ]);
    } catch (e) { failed = String(e?.message || e); }
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
async function transcribeAndSend(blob) {
  setState("thinking");
  const r = await api("/api/stt", { method: "POST", body: blob, raw: true }).catch(() => null);
  setState("idle");
  if (r?.text?.trim()) send(r.text, "voice");
  else if (r) note("I couldn't make out the words. Try again, a little closer to the phone.");
  else note("Transcription failed. Check Settings > Language models has a Groq or OpenAI key, or switch voice input to the phone.");
}

async function recordNative() {
  // In the app: record with Android's own microphone API (the in-app browser's microphone is unreliable).
  listening = true; setState("listening"); $("mic").classList.add("live");
  stopRecording = () => Native.stopRecord?.();
  let r = null, err = null;
  try { r = await Native.record({ maxMs: 15000 }); } catch (e) { err = String(e?.message || e); }
  stopRecording = null; listening = false; $("mic").classList.remove("live"); orb.level(0);
  if (err) {
    setState("idle");
    note(/permission/i.test(err) ? "JARVIS needs the microphone. Allow it in Settings > Apps > JARVIS > Permissions." : err);
    return;
  }
  if (!r?.heard) { setState("idle"); note("I didn't hear anything. Tap the mic and speak."); return; }
  const bin = atob(r.wav), bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  await transcribeAndSend(new Blob([bytes], { type: "audio/wav" }));
}

async function recordForServer() {
  // Records until you stop talking (or tap the mic), then transcribes on your server with Whisper.
  if (Native?.record) return recordNative();
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
    Native?.resumeWake?.();
    await transcribeAndSend(new Blob(chunks, { type: mr.mimeType || "audio/webm" }));
  };
}
$("mic").onclick = () => {
  if (listening && stopRecording) return stopRecording();   // tap again to finish early
  if (listening && rec) return rec.stop();
  if (listening) {                                          // phone recognizer busy: reset so the next tap works
    listening = false; $("mic").classList.remove("live"); setState("idle"); Native?.resumeWake?.();
    note("Stopped listening. Tap the mic again to talk.");
    return;
  }
  listen();
};

// ------------------------------------------------------------------ "Hey JARVIS" wake word
let wakeCtx = null, wakeStream = null;
async function setWake(on) {
  $("wake-toggle").setAttribute("aria-pressed", on);
  $("wake-label").textContent = on ? "Listening for Hey JARVIS" : "Hey JARVIS is off";
  store.set("jarvis_wake", on ? "1" : "0");
  if (Native?.wakeWord) {
    try { await Native.wakeWord({ enabled: on }); }
    catch (e) {
      $("wake-toggle").setAttribute("aria-pressed", "false");
      $("wake-label").textContent = "Hey JARVIS is off";
      store.set("jarvis_wake", "0");
      note(`Couldn't start Hey JARVIS: ${e?.message || e}`);
    }
    return;
  }
  if (!on) { wakeStream?.getTracks().forEach((t) => t.stop()); await wakeCtx?.close(); wakeCtx = null; return; }
  try {
    wakeStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    wakeCtx = new AudioContext();
    await wakeCtx.audioWorklet.addModule("/static/pcm-worklet.js");
    const src = wakeCtx.createMediaStreamSource(wakeStream);
    const node = new AudioWorkletNode(wakeCtx, "pcm-worklet");
    node.port.onmessage = (e) => {
      if (!listening) wsSend(e.data);
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

async function apiDetail(path, { method = "GET", body } = {}) {
  const headers = { Authorization: `Bearer ${token}` };
  if (body) headers["Content-Type"] = "application/json";
  let r;
  try { r = await fetch(SERVER + path, { method, headers, body: body && JSON.stringify(body) }); }
  catch { return { ok: false, error: "Can't reach the server." }; }
  if (r.status === 401) { unpair("The pairing token was rejected. Pair again."); return { ok: false, error: "Not paired." }; }
  const data = await r.json().catch(() => ({}));
  return r.ok ? { ok: true, data } : { ok: false, status: r.status, error: data?.detail || `Server error ${r.status}` };
}

// One live socket at a time. Every handler is bound to ITS socket, so a late event from an
// old socket (after the app was backgrounded, or the network switched) can't touch the new one.
let reconnectTimer = null;
function wsOpen() { return !!ws && ws.readyState === WebSocket.OPEN && wsReady; }
function wsSend(msg) {
  if (!wsOpen()) return false;
  try { ws.send(typeof msg === "string" || msg instanceof ArrayBuffer ? msg : JSON.stringify(msg)); return true; }
  catch { return false; }
}
function connect() {
  if (!token) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(reconnectTimer);
  const sock = new WebSocket(SERVER.replace(/^http/, "ws") + "/ws");
  ws = sock; wsReady = false;
  sock.binaryType = "arraybuffer";
  sock.onopen = () => { if (ws === sock) sock.send(JSON.stringify({ type: "auth", token, device: true })); };
  sock.onmessage = async (ev) => {
    if (ws !== sock) return;
    const m = JSON.parse(ev.data);
    if (m.type === "hello") { wsReady = true; retry = 0; setState("idle"); showRequests(m.pending || []); if (store.get("jarvis_wake") === "1") setWake(true); }
    else if (m.type === "error") unpair("The pairing token was rejected. Pair again.");
    else if (m.type === "reply") onReply(m);
    else if (m.type === "device_command") {
      const result = await runDevice(m.command, m.args);
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify({ type: "device_result", id: m.id, result }));
    }
    else if (m.type === "wake") onWake();
    else if (m.type === "pending_changed") api("/api/pending").then((r) => r && showRequests(r.pending)).catch(() => {});
    else if (m.type === "notify") { toast(m.text); if (m.speak) speak(m.text); }
  };
  sock.onclose = (e) => {
    if (ws !== sock) return;           // an old socket closing: ignore
    wsReady = false; ws = null;
    if (e.code === 4401) return;
    setState("offline");
    reconnectTimer = setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
  };
}
// Back from background / network changed: reconnect right away instead of waiting.
document.addEventListener("visibilitychange", () => { if (!document.hidden && token && !wsOpen()) { retry = 0; connect(); } });
window.addEventListener("online", () => { if (token && !wsOpen()) { retry = 0; connect(); } });

function unpair(msg) {
  secret.del(); token = null; started = false;
  const old = ws; ws = null; wsReady = false; old?.close();
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
  if (!wsSend({ type: "message", text, source })) api("/api/message", { method: "POST", body: { text, source } }).then((r) => r && onReply(r));
}
$("composer").onsubmit = (e) => { e.preventDefault(); const t = $("text").value; $("text").value = ""; send(t); };

function onReply(m) {
  setState("idle");
  if (m.understood) note(`Understood as: ${m.understood}`);
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
  if (!wsSend(wsMsg)) api(path, { method: "POST", body }).then((r) => r && onReply(r));
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
  if (Native?.biometric) {
    const ok = await Native.biometric({ reason: "Confirm this action" }).catch((e) => ({ error: e?.message || String(e) }));
    if (ok?.verified) finishApproval(true, "biometric");
    else note(`Not confirmed${ok?.error ? `: ${ok.error}` : ""}. Nothing was sent.`);
    return;
  }
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
// ------------------------------------------------------------------ language models (Settings)
let llmState = null;
function renderModels(state) {
  llmState = state;
  serverStt = !!state.server_stt;
  renderVoiceChoice();
  const sel = $("prov-type");
  // The list ships in the page (works even if the server is old or unreachable); the server's catalog
  // only adds entries and better labels.
  for (const c of state.catalog || []) {
    const o = [...sel.options].find((x) => x.value === c.type);
    if (o) o.textContent = c.label; else sel.append(new Option(c.label, c.type));
  }
  updateProviderForm();
  const ul = $("providers"); ul.replaceChildren();
  if (!state.providers.length) { ul.append(el("li", "empty", "No models yet. JARVIS uses its offline rule engine. Add a key below.")); return; }
  state.providers.forEach((p, i) => {
    const li = el("li", "prov" + (p.enabled ? "" : " off"));
    const info = el("span", "");
    info.append(el("span", "", `${i + 1}. ${p.label}`));
    const where = p.source === "env" ? "key from server .env" : `key ${p.key_hint}`;
    info.append(el("span", "sub", `${where}. Quick tasks: ${p.model_fast}. Conversation: ${p.model_smart}.`));
    if (p.last_error) info.append(el("span", "sub err-line", `Last error: ${p.last_error}`));
    li.append(info);
    const actions = el("span", "prov-actions");
    const btn = (label, title, fn) => { const b = el("button", "revoke", label); b.title = title; b.setAttribute("aria-label", title); b.onclick = fn; actions.append(b); return b; };
    if (i > 0) btn("Up", `Move ${p.label} up`, () => moveProvider(i, -1));
    btn(p.enabled ? "Off" : "On", p.enabled ? `Stop using ${p.label}` : `Use ${p.label}`, () => patchProvider(p.id, { enabled: !p.enabled }));
    btn("Models", `Choose models for ${p.label}`, () => chooseModels(p, li));
    if (p.source !== "env") btn("Remove", `Remove ${p.label}`, () => removeProvider(p));
    li.append(actions);
    ul.append(li);
  });
}
const BASE_NEEDED = new Set(["ollama", "custom"]);
function updateProviderForm() {
  const t = $("prov-type").value;
  const c = llmState?.catalog?.find((x) => x.type === t) || { needs_base: BASE_NEEDED.has(t), no_key: t === "ollama", help: "" };
  $("prov-base-wrap").classList.toggle("hidden", !c.needs_base);
  $("prov-key-wrap").classList.toggle("hidden", !!c.no_key);
  $("prov-help").textContent = c.help ? `Get a key: ${c.help}` : (c.no_key ? "No key needed. Enter the address where Ollama runs." : "");
}
$("prov-type").onchange = updateProviderForm;
function modelsMsg(t, ok = false) { const m = $("prov-msg"); m.textContent = t; m.classList.toggle("ok", ok); }
async function loadModels() {
  updateProviderForm();
  const r = await apiDetail("/api/llm");
  if (r.ok) renderModels(r.data);
  else modelsMsg(r.status === 404 ? "Your server is older than this app. Update it: git pull, then docker compose up -d --build."
                                  : `Couldn't load model settings: ${r.error}`);
}

// ------------------------------------------------------------------ connected accounts (Settings)
function openExternal(url) {
  if (Native?.openUrl) return Native.openUrl({ url }).catch((e) => accMsg(e?.message || String(e)));
  window.open(url, "_blank", "noopener");
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-ext]"); if (!a) return;
  e.preventDefault(); openExternal(a.href);
});
function accMsg(t, ok = false) { const m = $("acc-msg"); m.textContent = t; m.classList.toggle("ok", ok); }
let accState = null, accPoll = null, tokKind = null;
const TOKEN_HELP = {
  slack: { label: "Slack user token (xoxp-...)", help: "At api.slack.com/apps: Create New App, From scratch. OAuth & Permissions: add user scopes channels:history, channels:read, chat:write, im:history, users:read. Install to Workspace, then copy the User OAuth Token." },
  github: { label: "GitHub token (github_pat_...)", help: "At github.com/settings/personal-access-tokens: Generate new token (fine-grained), pick your repos, allow read on Actions, Issues, Pull requests and Metadata, and write on Issues." },
};
function renderAccounts(st) {
  accState = st;
  $("g-redirect").textContent = st.google.redirect_uri;
  const ul = $("accounts"); ul.replaceChildren();
  const row = (title, sub, actions) => {
    const li = el("li", "prov"); const info = el("span", "");
    info.append(el("span", "", title), el("span", "sub", sub));
    const box = el("span", "prov-actions");
    for (const [label, fn, cls] of actions) { const b = el("button", cls || "revoke", label); b.onclick = fn; box.append(b); }
    li.append(info, box); ul.append(li);
  };
  const g = st.google;
  row("Google (Gmail and Calendar)",
      g.connected ? `Connected: ${g.email || "your account"}` : (g.last_error || (g.client_saved ? "Ready. Tap Connect and sign in." : "Not connected")),
      g.connected ? [["Disconnect", () => disconnectAcc("google")]]
                  : g.client_saved ? [["Connect", () => connectGoogle(), "primary"], ["Setup", () => toggle("g-setup")]]
                                   : [["Connect", () => toggle("g-setup", true), "primary"]]);
  for (const k of ["slack", "github"]) {
    const a = st[k];
    const name = k === "slack" ? "Slack" : "GitHub";
    const sub = a.connected ? `Connected${a.team ? `: ${a.team}` : a.user ? `: ${a.user}` : ""}${a.from_env ? " (from server .env)" : ""}` : "Not connected";
    row(name, sub, a.connected && !a.from_env ? [["Disconnect", () => disconnectAcc(k)]]
                 : a.connected ? [] : [["Connect", () => showToken(k), "primary"]]);
  }
  if (g.connected && accPoll) { clearInterval(accPoll); accPoll = null; accMsg(`Google connected: ${g.email || ""}. Try "read my latest emails".`, true); $("g-setup").classList.add("hidden"); }
}
function toggle(id, show) { const n = $(id); n.classList.toggle("hidden", show === undefined ? !n.classList.contains("hidden") : !show); }
async function loadAccounts() {
  const r = await apiDetail("/api/accounts");
  if (r.ok) renderAccounts(r.data);
  else accMsg(r.status === 404 ? "Your server is older than this app. Update it to connect accounts." : r.error);
}
async function connectGoogle() {
  accMsg("Opening Google sign-in...");
  const r = await apiDetail("/api/accounts/google/start", { method: "POST", body: {} });
  if (!r.ok) { accMsg(r.error); return; }
  openExternal(r.data.url);
  accMsg("Sign in with Google in the browser, tick every box, then come back here.");
  clearInterval(accPoll);
  let n = 0;
  accPoll = setInterval(() => { if (++n > 100) { clearInterval(accPoll); accPoll = null; } loadAccounts(); }, 3000);
}
$("g-save").onclick = async () => {
  const id = $("g-id").value.trim(), sec = $("g-secret").value.trim();
  const body = id.startsWith("{") ? { json_text: id } : { client_id: id, client_secret: sec };
  const r = await apiDetail("/api/accounts/google/client", { method: "POST", body });
  if (!r.ok) { accMsg(r.error); return; }
  $("g-secret").value = ""; renderAccounts(r.data); connectGoogle();
};
$("g-redirect").onclick = () => navigator.clipboard?.writeText($("g-redirect").textContent).then(() => accMsg("Copied.", true)).catch(() => {});
function showToken(k) {
  tokKind = k; $("tok-label").textContent = TOKEN_HELP[k].label; $("tok-help").textContent = TOKEN_HELP[k].help;
  $("tok-val").value = ""; toggle("tok-setup", true); $("tok-val").focus();
}
$("tok-save").onclick = async () => {
  accMsg("Checking the token...");
  const r = await apiDetail(`/api/accounts/${tokKind}/connect`, { method: "POST", body: { token: $("tok-val").value.trim() } });
  $("tok-val").value = "";
  if (!r.ok) { accMsg(r.error); return; }
  toggle("tok-setup", false); accMsg("Connected.", true); renderAccounts(r.data);
};
async function disconnectAcc(k) {
  if (!confirm(`Disconnect ${k}? JARVIS deletes the stored sign-in.`)) return;
  const r = await apiDetail(`/api/accounts/${k}/disconnect`, { method: "POST", body: {} });
  if (r.ok) { accMsg("Disconnected.", true); renderAccounts(r.data); } else accMsg(r.error);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && accPoll) loadAccounts(); });
$("prov-form").onsubmit = async (e) => {
  e.preventDefault();
  const body = { type: $("prov-type").value, api_key: $("prov-key").value.trim(), base_url: $("prov-base").value.trim() };
  const btnEl = $("prov-save"); btnEl.disabled = true; modelsMsg("Testing the key with a real request...");
  const r = await apiDetail("/api/llm/providers", { method: "POST", body });
  btnEl.disabled = false;
  if (!r.ok) { modelsMsg(r.error); return; }
  $("prov-key").value = "";                       // never keep the key on the phone
  const m = r.data.result?.models || {};
  modelsMsg(`Works. Now first in line. Quick tasks: ${m.fast}. Conversation: ${m.smart}.`, true);
  renderModels(r.data);
};
async function patchProvider(id, body) {
  modelsMsg(body.model_fast || body.model_smart ? "Testing that model..." : "");
  const r = await apiDetail(`/api/llm/providers/${encodeURIComponent(id)}`, { method: "POST", body });
  if (!r.ok) { modelsMsg(r.error); return; }
  if (body.model_fast || body.model_smart) modelsMsg("Model switched.", true);
  renderModels(r.data);
}
async function removeProvider(p) {
  if (!confirm(`Remove ${p.label}? Its key is deleted from the server.`)) return;
  const r = await apiDetail(`/api/llm/providers/${encodeURIComponent(p.id)}`, { method: "DELETE" });
  if (r.ok) { modelsMsg(`${p.label} removed.`, true); renderModels(r.data); } else modelsMsg(r.error);
}
async function moveProvider(i, d) {
  const ids = llmState.providers.map((p) => p.id);
  [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
  const r = await apiDetail("/api/llm/order", { method: "POST", body: { ids } });
  if (r.ok) renderModels(r.data); else modelsMsg(r.error);
}
async function chooseModels(p, li) {
  const old = li.querySelector(".pick"); if (old) { old.remove(); return; }
  const box = el("div", "pick");
  box.append(el("span", "sub", "Loading the models this key can use..."));
  li.append(box);
  const r = await apiDetail(`/api/llm/providers/${encodeURIComponent(p.id)}/models`);
  box.replaceChildren();
  if (!r.ok) { box.append(el("span", "sub err-line", r.error)); return; }
  const make = (label, current, field) => {
    const lab = el("label", "", label); const s2 = el("select"); s2.id = `${p.id}-${field}`; lab.htmlFor = s2.id;
    s2.append(new Option("Automatic (best available)", "auto"));
    for (const m of r.data.models) s2.append(new Option(m, m));
    s2.value = r.data.models.includes(current) ? current : "auto";
    box.append(lab, s2); return s2;
  };
  const f = make("Quick tasks (routing, commands)", p.configured_fast, "fast");
  const g = make("Conversation and planning", p.configured_smart, "smart");
  const save = el("button", "primary", "Save models");
  save.onclick = () => patchProvider(p.id, { model_fast: f.value, model_smart: g.value });
  box.append(save);
}

// ------------------------------------------------------------------ in-app updates
let myVersion = null, updateInfo = null, updateOffered = false;
async function refreshUpdate(force = false) {
  if (Native?.appVersion) myVersion = await Native.appVersion().catch(() => null);
  const r = force ? await apiDetail("/api/app/check", { method: "POST" }) : await apiDetail("/api/app/update");
  updateInfo = r.ok ? r.data : null;
  renderUpdate(r.ok ? null : r.error);
  return updateInfo;
}
function updateIsNewer() {
  const a = updateInfo?.available;
  return !!(a && myVersion && a.code > myVersion.code);
}
function renderUpdate(err) {
  const info = $("upd-info"); if (!info) return;
  const a = updateInfo?.available;
  const mine = myVersion ? `This app: 2.${myVersion.code}.` : "";
  if (updateInfo?.disabled) info.textContent = "Updates are turned off on the server.";
  else if (updateIsNewer()) info.textContent = `${mine} Update 2.${a.code} is ready (${Math.round(a.size / 1e6)} MB).`;
  else if (a) info.textContent = `${mine} You're up to date.`;
  else info.textContent = `${mine} No update prepared yet.`;
  $("upd-install").classList.toggle("hidden", !updateIsNewer());
  $("upd-download").classList.toggle("hidden", IN_APP || !a);
  const e = err || updateInfo?.last_error;
  $("upd-msg").textContent = e ? `Server: ${e}` : "";
}
async function installUpdate() {
  const a = updateInfo?.available; if (!a) return;
  $("upd-msg").textContent = "Downloading from your server...";
  let r;
  try { r = await Native.installUpdate({ url: SERVER + "/api/app/apk", token, sha256: a.sha256, code: a.code }); }
  catch (e) { $("upd-msg").textContent = String(e?.message || e); return; }
  const why = {
    allow_installs: "Allow JARVIS to install updates in the screen that just opened (one time), then tap Install update again.",
    different_signer: "This app was installed from an older test build, so Android won't accept the update over it. One last time: uninstall JARVIS, then open your server address in Chrome, Settings > Download the Android app, and install it. From then on, updates are one tap here.",
    not_newer: "You already have this version.", invalid_apk: "The download isn't a valid app.", wrong_package: "That file isn't JARVIS.",
  };
  $("upd-msg").textContent = r?.ok ? "Android will now ask you to confirm the update. Tap Update." : (why[r?.error] || `Update failed: ${r?.error}`);
}
$("upd-install").onclick = installUpdate;
$("upd-check").onclick = async () => { $("upd-msg").textContent = "Checking GitHub and preparing the update on your server..."; await refreshUpdate(true); };
$("upd-download").onclick = async (e) => {             // browser only: one-time install of the server-signed app
  e.preventDefault();
  $("upd-msg").textContent = "Downloading...";
  const r = await fetch(SERVER + "/api/app/apk", { headers: { Authorization: `Bearer ${token}` } });
  if (!r.ok) { $("upd-msg").textContent = "No app prepared yet. Tap Check for updates."; return; }
  const url = URL.createObjectURL(await r.blob());
  const a = document.createElement("a"); a.href = url; a.download = "jarvis.apk"; document.body.append(a); a.click(); a.remove();
  $("upd-msg").textContent = "Downloaded. Open jarvis.apk from your downloads to install.";
};
if (Native?.addListener) Native.addListener("updateProgress", (e) => { $("upd-msg").textContent = `Downloading ${Math.round((e.progress || 0) * 100)}%`; });
function offerUpdateBanner() {
  if (updateOffered || !IN_APP || !updateIsNewer()) return;
  updateOffered = true;
  const li = el("li", "banner");
  li.append(el("span", "", `JARVIS 2.${updateInfo.available.code} is ready.`));
  const b = el("button", "primary", "Update");
  b.onclick = () => { document.querySelector('#tabs button[data-view=security]').click(); installUpdate(); };
  li.append(b);
  $("log").append(li);
}

// ------------------------------------------------------------------ morning call, about you, voice
function briefMsg(t, ok) { const m = $("brief-msg"); m.textContent = t; m.classList.toggle("ok", !!ok); }
async function syncBrief(b) {
  // The alarm lives on the phone (works even when the app is closed); the server only needs the time
  // to know when "overnight" is, for reply drafts.
  if (!Native?.scheduleBrief || !b) return;
  const [hour, minute] = b.time.split(":").map(Number);
  await Native.scheduleBrief({ enabled: !!b.enabled, hour, minute }).catch(() => {});
}
async function loadPersonal() {
  const b = await apiDetail("/api/brief");
  if (b.ok) { $("brief-on").checked = b.data.enabled; $("brief-time").value = b.data.time; $("brief-drafts").checked = b.data.drafts; }
  const p = await apiDetail("/api/profile");
  if (p.ok) $("about").value = p.data.about;
  loadVoices();
}
$("brief-save").onclick = async () => {
  const body = { enabled: $("brief-on").checked, time: $("brief-time").value || "06:00", drafts: $("brief-drafts").checked };
  const r = await apiDetail("/api/brief", { method: "POST", body });
  if (!r.ok) { briefMsg(r.error); return; }
  await syncBrief(r.data);
  briefMsg(r.data.enabled ? `I'll call you at ${r.data.time} every day.` : "Morning call is off.", true);
};
$("about-save").onclick = async () => {
  const r = await apiDetail("/api/profile", { method: "POST", body: { about: $("about").value } });
  const m = $("about-msg");
  m.textContent = r.ok ? (r.data.about !== $("about").value.trim() ? "Saved. I removed codes or numbers that shouldn't be stored." : "Saved.") : r.error;
  m.classList.toggle("ok", r.ok);
  if (r.ok) $("about").value = r.data.about;
};
async function loadVoices() {
  const sel = $("voice"); sel.replaceChildren();
  let list = [], current = null;
  if (Native?.getVoices) {
    const r = await Native.getVoices().catch(() => null);
    list = (r?.voices || []).map((v) => ({ name: v.name, label: `${v.locale}${v.network ? " (online)" : ""}: ${v.name}` }));
    current = r?.current;
  } else if (window.speechSynthesis) {
    list = window.speechSynthesis.getVoices().filter((v) => /^en/i.test(v.lang)).map((v) => ({ name: v.name, label: `${v.lang}: ${v.name}` }));
    current = store.get("jarvis_voice");
  }
  const sv = await apiDetail("/api/tts/voices");
  if (sv.ok && sv.data.voices.length) {
    const g = document.createElement("optgroup"); g.label = "JARVIS server voices";
    for (const v of sv.data.voices) g.append(new Option(v.label, `server:${v.id}`));
    sel.append(g);
  }
  const g2 = document.createElement("optgroup"); g2.label = "Phone voices";
  g2.append(new Option("Phone default voice", ""));
  // British voices first: the movie JARVIS is British.
  list.sort((a, b) => (/en-GB/i.test(b.label) - /en-GB/i.test(a.label)) || a.label.localeCompare(b.label));
  for (const v of list) g2.append(new Option(v.label, v.name));
  sel.append(g2);
  const vals = [...sel.options].map((o) => o.value);
  sel.value = serverVoice && vals.includes(`server:${serverVoice}`) ? `server:${serverVoice}`
            : current && list.some((v) => v.name === current) ? current : "";
}
$("voice").onchange = async () => {
  const name = $("voice").value;
  if (name.startsWith("server:")) { serverVoice = name.slice(7); store.set("jarvis_server_voice", serverVoice); return; }
  serverVoice = ""; store.set("jarvis_server_voice", "");
  if (Native?.setVoice) await Native.setVoice({ name: name || null }).catch(() => {});
  else { store.set("jarvis_voice", name); pickVoice(); }
};
$("voice-test").onclick = () => { muted = false; speak(`Good morning, ${myName || "sir"}. Nothing needed you overnight.`); };
let myName = null;
// The morning call fired and you tapped "Brief me" (or opened the app from it).
async function onBriefCall() {
  document.querySelector('#tabs button[data-view=main]')?.click();
  for (let i = 0; i < 40 && !wsOpen(); i++) await new Promise((r) => setTimeout(r, 250));   // connection settles
  send("overnight report", "brief");
}
if (Native?.addListener) Native.addListener("brief", () => onBriefCall());

function renderVoiceChoice() {
  document.querySelectorAll("input[name=stt]").forEach((r) => {
    r.checked = r.value === sttMode;
    r.onchange = () => { sttMode = r.value; store.set("jarvis_stt", sttMode); };
  });
  $("stt-note").textContent = serverStt ? "" : "Whisper needs a Groq or OpenAI key (Language models below); until then the phone recognizer is used.";
}
async function loadSecurity() {
  renderVoiceChoice();
  loadModels();
  loadAccounts();
  refreshUpdate();
  loadPersonal();
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

// ------------------------------------------------------------------ never fail silently
let lastErr = "";
function reportError(msg) {
  msg = String(msg || "unknown error").slice(0, 200);
  if (msg === lastErr) return;
  lastErr = msg;
  try { note(`Something went wrong: ${msg}`); } catch {}
}
window.addEventListener("error", (e) => reportError(e.message));
window.addEventListener("unhandledrejection", (e) => reportError(e.reason?.message || e.reason));

// ------------------------------------------------------------------ boot
let started = false;
function start() {
  if (started) { connect(); return; }
  started = true;
  $("pair").classList.add("hidden"); $("main").classList.remove("hidden"); $("tabs").classList.remove("hidden");
  setState("offline"); renderStatus(); renderClock();
  note("Say Hey JARVIS, tap the mic, or type.");
  api("/api/status").then((st) => {
    serverStt = !!st?.server_stt; myName = st?.user || null;
    if (serverVoice == null) serverVoice = st?.server_voice ? "jarvis" : "";
  }).catch(() => {});
  apiDetail("/api/brief").then((r) => r.ok && syncBrief(r.data)).catch(() => {});   // keep the phone alarm in sync
  setTimeout(() => refreshUpdate().then(offerUpdateBanner).catch(() => {}), 3000);
  connect();
}
if (!IN_APP) $("server-field").classList.add("hidden");
if (Native?.addListener) Native.addListener("level", (e) => orb.level(e.level || 0));
secret.get().then((t) => { token = t; if (token && SERVER) start(); else $("tabs").classList.add("hidden"); });
window.__jarvis = { send, phone, setWake, listen, speakOff: () => (muted = true),
  setStt: (mode, available) => { sttMode = mode; serverStt = available; },
  dropConnection: () => ws?.close() }; // used by automated tests
