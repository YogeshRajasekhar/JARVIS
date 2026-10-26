"""
Browser end-to-end test of the virtual phone: real Chromium, real server, real WebSocket,
real microphone pipeline (Chromium's fake mic plays "hey jarvis" audio), real wake-word model.

Run:  pytest tests_v2/test_e2e_phone.py   (needs: pip install playwright && playwright install chromium)
Screenshots go to tests_v2/artifacts/.
"""

import os
import socket
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
HERE = os.path.dirname(__file__)
MODELS = os.path.join(os.path.dirname(HERE), "models")
ART = os.path.join(HERE, "artifacts")
TOKEN = "e2e-token-0123456789"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server():
    import uvicorn
    from conftest import NOW, offline_settings
    from jarvis.core.storage import Database
    from jarvis.integrations.device import RemoteDevice
    from jarvis.server.app import create_app
    from jarvis.services import build_services

    s = offline_settings()
    s.device_token = TOKEN
    svc = build_services(s, db=Database(":memory:"), clock=lambda: NOW, seed=True, device=RemoteDevice(lambda m: None))
    app = create_app(s, svc=svc, enable_wakeword=True, model_dir=MODELS)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", svc
    srv.should_exit = True


@pytest.fixture(scope="module")
def page(server):
    url, _ = server
    os.makedirs(ART, exist_ok=True)
    with pw.sync_playwright() as p:
        b = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={os.path.join(HERE, 'data', 'hey_jarvis_loop.wav')}",
            "--autoplay-policy=no-user-gesture-required"])
        ctx = b.new_context(viewport={"width": 430, "height": 900}, bypass_csp=True)  # test harness only: our strict CSP blocks eval
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url + "/")
        pg.fill("#pair-token", TOKEN)
        pg.click("#pair-form .primary")
        pg.wait_for_selector("#state:has-text('Ready')", timeout=10000)
        pg.evaluate("window.__jarvis.speakOff()")
        pg.errors = errors
        yield pg
        b.close()


def say(pg, text, wait=900):
    n = pg.locator("#log li.jarvis").count()
    pg.fill("#text", text)
    pg.press("#text", "Enter")
    pg.wait_for_function(f"document.querySelectorAll('#log li.jarvis').length > {n}", timeout=8000)
    pg.wait_for_timeout(wait)
    return pg.locator("#log li.jarvis").last.inner_text()


def allow(pg):
    pg.wait_for_selector("#perm:not(.hidden)", timeout=5000)
    pg.click("#perm button[data-mode=always]")


def test_pairing_rejects_wrong_token(server):
    url, _ = server
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(url + "/")
        pg.fill("#pair-token", "definitely-wrong")
        pg.click("#pair-form .primary")
        pg.wait_for_selector("#pair-err:has-text(\"doesn't match\")", timeout=5000)
        b.close()


def test_day_in_the_life(page, server):
    _, svc = server
    pg = page
    # 1. Calendar needs permission the first time (Android-style prompt), then answers.
    say(pg, "what's on my calendar today", wait=300)
    allow(pg)
    pg.wait_for_selector("#log li.jarvis:has-text('Lab standup')", timeout=5000)
    pg.screenshot(path=os.path.join(ART, "1_calendar.png"))

    # 2. Alarm is executed by the phone (JS), not the server: check the Clock app.
    say(pg, "wake me up at 6:30", wait=300)
    allow(pg)
    pg.wait_for_selector("#log li.jarvis:has-text('6:30 AM')", timeout=5000)
    assert pg.evaluate("window.__jarvis.phone.alarms.length") == 1

    # 3. Email: contacts permission, mail permission, then approval sheet. Edit then approve.
    say(pg, "email Arun that the GPU swap is fine for tomorrow 2 to 5pm", wait=300)
    for _ in range(3):
        if pg.is_visible("#perm"):
            pg.click("#perm button[data-mode=always]")
            pg.wait_for_timeout(500)
    pg.wait_for_selector("#appr:not(.hidden)", timeout=5000)
    assert "arun.demo@example.com" in pg.inner_text("#appr-preview")
    pg.screenshot(path=os.path.join(ART, "2_approval.png"))
    assert svc.mail.sent == []
    pg.click("#appr-editbtn")
    pg.fill("#edit-body", "Hi Arun,\n\nGPU swap works for me, 2 to 5pm tomorrow.\n\nBest,\nYogesh")
    pg.click("#appr-yes")
    pg.wait_for_selector("#log li.jarvis:has-text('Sent')", timeout=5000)
    assert svc.mail.sent[-1]["body"].startswith("Hi Arun,\n\nGPU swap works")

    # 4. Conflict -> slot chips -> tap a chip -> approval -> booked.
    svc.broker.grant("calendar.write", "always")
    say(pg, "set up a meeting with Priya tomorrow at 11 for an hour")
    pg.wait_for_selector(".slots button", timeout=5000)
    pg.screenshot(path=os.path.join(ART, "3_slots.png"))
    pg.locator(".slots button").first.click()
    pg.wait_for_selector("#appr:not(.hidden)", timeout=5000)
    pg.click("#appr-yes")
    pg.wait_for_selector("#log li.jarvis:has-text('Booked')", timeout=5000)

    # 5. Phishing email is flagged and never acted on.
    say(pg, "any unread mails?", wait=300)
    allow(pg)
    pg.wait_for_selector("#log li.jarvis:has-text('phishing')", timeout=5000)
    assert "551234" not in pg.locator("#log li.jarvis").last.inner_text()

    # 6. Blocked app and voice-unlock refusal.
    assert "don't open" in say(pg, "open gpay")
    # 7. Lockdown from the Security tab.
    pg.click("#tabs button[data-view=security]")
    pg.wait_for_selector("#perms li", timeout=5000)
    pg.screenshot(path=os.path.join(ART, "4_security.png"))
    pg.click("#lockdown")
    pg.wait_for_selector("#unlock:not(.hidden)", timeout=5000)
    assert svc.db.all_grants() == {}
    pg.click("#tabs button[data-view=main]")
    assert pg.errors == []


def test_wake_word_from_microphone(page):
    """Chromium's fake mic loops 'hey jarvis'; the page streams PCM to the server, the
    openWakeWord model fires, and the page reacts (wake counter increments)."""
    if not os.path.exists(os.path.join(MODELS, "hey_jarvis_v0.1.onnx")):
        pytest.skip("wake-word models not downloaded")
    pg = page
    pg.click("#wake-toggle")
    pg.wait_for_function("(window.__wakes || 0) >= 1", timeout=15000)
    pg.screenshot(path=os.path.join(ART, "5_wake.png"))
    pg.click("#wake-toggle")


def test_whisper_voice_input_records_until_silence(page, server):
    """Mic -> record until the speaker stops -> server Whisper -> answer. Fake mic plays 'hey jarvis'."""
    import jarvis.voice.speech as speech
    calls = []

    class FakeWhisper:
        def transcribe(self, audio, filename, prompt=None):
            calls.append(len(audio))
            return "what's my battery"

    speech_build = speech.build_stt
    speech.build_stt = lambda *a, **k: FakeWhisper()
    try:
        pg = page
        pg.reload()
        pg.wait_for_selector("#state:has-text('Ready')", timeout=10000)
        pg.evaluate("window.__jarvis.speakOff()")
        pg.evaluate("window.__jarvis.setStt('whisper', true)")
        n = pg.locator("#log li.jarvis").count()
        pg.click("#mic")
        try:
            pg.wait_for_function(f"document.querySelectorAll('#log li.jarvis').length > {n}", timeout=20000)
        except Exception:
            raise AssertionError("no reply; last notes: " + " | ".join(pg.locator("#log li.note").all_inner_texts()[-3:]))
        assert calls and calls[0] > 1000          # real recorded audio reached the server
        assert pg.locator("#log li.you").last.inner_text() == "what's my battery"
    finally:
        speech.build_stt = speech_build


def test_works_without_browser_speech_apis(server, page):
    """Android's WebView has no speechSynthesis/SpeechRecognition. Tapping the mic must not crash."""
    url, _ = server
    if True:  # reuse the module's browser (Playwright allows one sync instance per thread)
        ctx = page.context.browser.new_context(bypass_csp=True)
        ctx.add_init_script("""
            for (const k of ['speechSynthesis', 'SpeechSynthesisUtterance', 'SpeechRecognition', 'webkitSpeechRecognition']) {
              try { delete window[k]; } catch {}
              try { delete Window.prototype[k]; } catch {}
            }
        """)
        pg = ctx.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url + "/")
        assert pg.evaluate("typeof speechSynthesis") == "undefined"
        pg.fill("#pair-token", TOKEN)
        pg.click("#pair-form .primary")
        pg.wait_for_selector("#state:has-text('Ready')", timeout=10000)
        pg.click("#mic")
        pg.wait_for_timeout(1500)
        pg.fill("#text", "hello")
        pg.press("#text", "Enter")
        pg.wait_for_selector("#log li.jarvis", timeout=8000)
        notes = " ".join(pg.locator("#log li.note").all_inner_texts())
        assert errors == [] and "Something went wrong" not in notes, (errors, notes)
        ctx.close()


def test_reconnect_does_not_crash(server, page):
    """Network drop + typing during reconnect: no 'Still in CONNECTING state' crash, message still answered."""
    pg = page
    pg.reload()
    pg.wait_for_selector("#state:has-text('Ready')", timeout=10000)
    pg.evaluate("window.__jarvis.speakOff()")
    before = len(pg.errors)
    pg.evaluate("window.__jarvis.dropConnection()")                     # like the app being backgrounded
    pg.wait_for_selector("#state:has-text('Reconnecting')", timeout=10000)
    pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # app comes back: reconnect now
    n = pg.locator("#log li.jarvis").count()
    pg.fill("#text", "hello")        # sent while the new socket is still CONNECTING
    pg.press("#text", "Enter")
    pg.wait_for_function(f"document.querySelectorAll('#log li.jarvis').length > {n}", timeout=10000)
    pg.wait_for_selector("#state:has-text('Ready')", timeout=15000)
    notes = " ".join(pg.locator("#log li.note").all_inner_texts())
    assert pg.errors[before:] == [] and "CONNECTING" not in notes, (pg.errors[before:], notes)


def test_settings_add_model_provider(page, server, monkeypatch):
    """Settings > Language models: paste a key, it's tested, added first, and the key field is wiped."""
    from jarvis.llm.registry import LLMRegistry
    from test_providers import GOOD_KEY, FakeLLM

    monkeypatch.setattr(LLMRegistry, "client", lambda self, e, tier, key=None: FakeLLM(e, tier, self._key_for(e) if key is None else key))
    _, svc = server
    if svc.executor.locked_down:
        svc.executor.unlock()
    monkeypatch.setattr(svc.settings, "llm_mode", "auto")   # the e2e server otherwise runs offline
    pg = page
    pg.reload()
    pg.wait_for_selector("#state:has-text('Ready')", timeout=10000)
    pg.click("#tabs button[data-view=security]")
    pg.wait_for_selector("#prov-type option[value=openai]", state="attached", timeout=8000)
    pg.select_option("#prov-type", "openai")
    pg.fill("#prov-key", "sk-wrong-key-00000000")
    pg.click("#prov-save")
    pg.wait_for_selector("#prov-msg:has-text('rejected')", timeout=8000)
    pg.fill("#prov-key", GOOD_KEY)
    pg.click("#prov-save")
    pg.wait_for_selector("#prov-msg:has-text('Works')", timeout=8000)
    assert pg.input_value("#prov-key") == ""
    first = pg.locator("#providers li").first.inner_text()
    assert "OpenAI" in first and "openai-smart-model" in first and GOOD_KEY not in pg.content()
    pg.screenshot(path=os.path.join(ART, "6_models.png"))
    assert svc.smart_llm.complete("hi") == "answer from openai-smart-model"
    # New sections render and save without errors.
    pg.check("#brief-on")
    pg.fill("#brief-time", "05:45")
    pg.click("#brief-save")
    pg.wait_for_selector("#brief-msg:has-text('05:45')", timeout=5000)
    pg.fill("#about", "Master's student. Keep emails short.")
    pg.click("#about-save")
    pg.wait_for_selector("#about-msg:has-text('Saved')", timeout=5000)
    assert pg.locator("#voice option").count() >= 1
    assert "Update" in pg.inner_text("#upd-info") or "update" in pg.inner_text("#upd-info") or "off" in pg.inner_text("#upd-info")
    pg.screenshot(path=os.path.join(ART, "7_settings.png"), full_page=True)
    assert pg.errors == [], pg.errors
    pg.click("#tabs button[data-view=main]")
