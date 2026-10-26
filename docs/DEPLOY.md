# Putting JARVIS on your phone

Everything below is free. Total time: about an hour, most of it waiting for sign-ups.

```
 Your Android phone                          Free cloud VM (always on)
 ┌──────────────────────┐   Tailscale VPN    ┌──────────────────────────────┐
 │ JARVIS app           │  (private HTTPS,   │ tailscale  ─►  jarvis server │
 │  wake word on-device │◄──── no open ─────►│                SQLite volume │
 │  speech, TTS, prompts│      ports)        │ daily DB backup (7 kept)     │
 └──────────────────────┘                    └──────────────────────────────┘
```

## 1. Free API keys (5 min)

1. Groq: https://console.groq.com/keys, create a key. This is the main brain.
2. Gemini: https://aistudio.google.com/app/apikey, create a key. Used when Groq is rate-limited.
3. Optional: OpenRouter key as a third fallback.

Keep the keys private; they go only into `deploy/.env` on the VM.

## 2. Tailscale (5 min)

1. Sign up at https://tailscale.com (free personal plan), install the Tailscale app on your phone and log in.
2. Admin console, DNS: turn on MagicDNS and HTTPS certificates.
3. Settings, Keys: generate an auth key (reusable off, ephemeral off). This is `TS_AUTHKEY`.

Tailscale is what makes the server unreachable from the public internet while your phone can
reach it from anywhere, including Germany.

## 3. Free VM (15 min)

Either Oracle Cloud Always Free (Ampere A1, generous) or Google Cloud e2-micro (us regions).
Create an Ubuntu 22.04 or 24.04 VM with SSH-key login, then on the VM:

```bash
git clone https://github.com/YogeshRajasekhar/JARVIS.git && cd JARVIS && git checkout v2
cd deploy && sudo ./setup_vm.sh          # firewall, auto security updates, Docker, .env with fresh secrets
nano .env                                # paste GROQ_API_KEY and GEMINI_API_KEY
sudo TS_AUTHKEY=tskey-auth-xxxx docker compose up -d --build
sudo docker compose logs jarvis | grep "Pairing token"
```

The server is now at `https://jarvis.<your-tailnet>.ts.net` (shown in the Tailscale admin console).

## 4. Connect your accounts (optional, 15 min)

- Google Calendar + Gmail: follow the header of `scripts/google_auth.py` (create a Desktop OAuth
  client, then run the script once). Scopes are limited to calendar events, read mail and send mail.
  Then set `JARVIS_SEED_DEMO=0` and restart.
- Slack: create an app at api.slack.com, add the user scopes listed in `.env.example`, install it
  to your workspace and put the `xoxp-` token in `SLACK_USER_TOKEN`.
- GitHub: create a fine-grained token (read: issues, pull requests, actions, notifications;
  write: issues) and put it in `GITHUB_TOKEN`.

Restart: `sudo docker compose restart jarvis`. `GET /api/status` shows which integrations are live.

## 5. The Android app (10 min)

The repo's GitHub Action builds the APK in the cloud on every push to `v2`:

1. GitHub, Actions, "Android APK", the latest run, download `jarvis-debug-apk`.
2. On the phone, open the APK and allow "install unknown apps" for your browser/files app once.
3. Open JARVIS, enter `https://jarvis.<your-tailnet>.ts.net` and the pairing token.
4. Switch on "Hey JARVIS". Android asks for the microphone and notifications; a persistent
   notification shows while it listens. Audio for the wake word never leaves the phone.
5. The first time JARVIS needs something (calendar, contacts, location, notifications) you get
   JARVIS's prompt and then Android's own prompt. Both can be revoked later.

Notification reading and Do Not Disturb need one extra switch each in Android settings; JARVIS
opens the right screen the first time you ask.

## 6. Check it works

- "Hey JARVIS, what's on my calendar today?"
- "Email Arun that I'll be ten minutes late" (approval card appears; nothing is sent until you tap)
- Security tab: permissions you granted, recent activity, and the lockdown button.

## Limits worth knowing

- Android does not let apps switch Wi-Fi or Bluetooth directly; JARVIS opens the system panel.
- Calls open the dialer and SMS opens Messages with the approved text; you press call/send.
  JARVIS deliberately has no permission to call or text silently.
- When the screen is off, a wake word opens JARVIS through a full-screen notification
  (the only way Android allows); on some phones you may need to tap it.
- Free model limits: roughly a thousand requests a day on Groq's larger model. The keyword fast
  path and caching keep everyday use far below that; when limits hit, JARVIS falls back automatically.
- The pre-trained "hey jarvis" model is licensed for non-commercial use, which covers a personal assistant.


## App updates (no more reinstalling)

Every push builds a new app version. Your server picks it up within 6 hours (or immediately with
Settings > App updates > Check for updates), signs it with a key that lives only on your server, and the app
offers it: tap **Update**, then Android's **Update** confirmation. Your pairing and settings are kept.

One-time switch-over from the old test builds (their signature is different, so Android can't update over them):
1. Update the server (`git pull` and `docker compose up -d --build`), then wait a minute.
2. Uninstall the old JARVIS app.
3. In Chrome on the phone, open `https://jarvis.<tailnet>.ts.net`, pair, go to Settings > App updates >
   **Download the Android app**, and install it.
4. The first time you tap Update later, Android asks to allow JARVIS to install apps. Allow it once.

Keep backups of the server's data volume: it holds the signing key. If it is lost, the app has to be
reinstalled once more with a new key.
