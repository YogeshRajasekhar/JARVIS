package in.yogesh.jarvis;

import android.Manifest;
import android.app.AlarmManager;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.database.Cursor;
import android.hardware.camera2.CameraManager;
import android.location.Location;
import android.location.LocationManager;
import android.media.AudioManager;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.provider.AlarmClock;
import android.provider.ContactsContract;
import android.provider.Settings;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;

import androidx.biometric.BiometricManager;
import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.Executor;
import java.util.regex.Pattern;

/**
 * Native side of JARVIS. Exposed to the web UI as window.Capacitor.Plugins.Jarvis.
 *
 * Two permission layers protect the phone:
 *  1. JARVIS scopes (server-side permission broker + approvals) decide WHETHER an action may happen.
 *  2. Android runtime permissions (this class) are requested the first time a capability is used,
 *     with the normal system dialog, and can be revoked in Android settings at any time.
 * Banking / payment / password apps are refused here too (defence in depth).
 */
@CapacitorPlugin(
        name = "Jarvis",
        permissions = {
                @Permission(alias = "microphone", strings = {Manifest.permission.RECORD_AUDIO}),
                @Permission(alias = "contacts", strings = {Manifest.permission.READ_CONTACTS}),
                @Permission(alias = "location", strings = {Manifest.permission.ACCESS_COARSE_LOCATION}),
                @Permission(alias = "notifications", strings = {"android.permission.POST_NOTIFICATIONS"})
        })
public class JarvisPlugin extends Plugin {
    private static volatile boolean appVisible = false;
    private static final Pattern BLOCKED_APPS = Pattern.compile(
            "g ?pay|google pay|phone ?pe|paytm|bhim|cred\\b|amazon pay|bank|sbi|yono|hdfc|icici|axis|kotak|zerodha|kite|groww|upstox|"
                    + "wallet|crypto|binance|coinbase|1password|lastpass|bitwarden|keepass|authenticator|password|digilocker|aadhaar",
            Pattern.CASE_INSENSITIVE);

    private TextToSpeech tts;
    private boolean ttsReady = false;
    private BroadcastReceiver wakeReceiver;

    public static boolean isAppVisible() { return appVisible; }

    @Override
    public void load() {
        wakeReceiver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) { notifyListeners("wake", new JSObject(), true); }
        };
        ContextCompat.registerReceiver(getContext(), wakeReceiver, new IntentFilter(WakeWordService.EVENT_WAKE),
                ContextCompat.RECEIVER_NOT_EXPORTED);
        tts = new TextToSpeech(getContext(), status -> {
            ttsReady = status == TextToSpeech.SUCCESS;
            if (ttsReady) {
                tts.setLanguage(new Locale("en", "IN"));
                tts.setSpeechRate(1.02f);
            }
        });
        Intent launch = getActivity().getIntent();
        if (launch != null && launch.getBooleanExtra("jarvis_wake", false)) notifyListeners("wake", new JSObject(), true);
    }

    @Override protected void handleOnResume() { appVisible = true; }
    @Override protected void handleOnPause() { appVisible = false; }

    @Override
    protected void handleOnNewIntent(Intent intent) {
        if (intent != null && intent.getBooleanExtra("jarvis_wake", false)) notifyListeners("wake", new JSObject(), true);
    }

    @Override
    protected void handleOnDestroy() {
        try { getContext().unregisterReceiver(wakeReceiver); } catch (Exception ignored) { }
        if (tts != null) tts.shutdown();
    }

    private void wakeService(String action) {
        Intent i = new Intent(getContext(), WakeWordService.class).setAction(action);
        if (WakeWordService.ACTION_START.equals(action)) ContextCompat.startForegroundService(getContext(), i);
        else getContext().startService(i);
    }

    // ------------------------------------------------------------------ wake word
    @PluginMethod
    public void wakeWord(PluginCall call) {
        boolean enabled = Boolean.TRUE.equals(call.getBoolean("enabled", false));
        if (!enabled) {
            wakeService(WakeWordService.ACTION_STOP);
            call.resolve();
            return;
        }
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "wakePermCallback");
            return;
        }
        if (Build.VERSION.SDK_INT >= 33 && getPermissionState("notifications") != PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "wakePermCallback");
            return;
        }
        wakeService(WakeWordService.ACTION_START);
        call.resolve();
    }

    @PermissionCallback
    private void wakePermCallback(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            call.reject("Microphone permission denied");
            return;
        }
        wakeWord(call);
    }

    // ------------------------------------------------------------------ speech to text (on-device where available)
    @PluginMethod
    public void listen(PluginCall call) {
        if (getPermissionState("microphone") != PermissionState.GRANTED) {
            requestPermissionForAlias("microphone", call, "listenPermCallback");
            return;
        }
        wakeService(WakeWordService.ACTION_PAUSE);
        getActivity().runOnUiThread(() -> {
            SpeechRecognizer sr = (Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(getContext()))
                    ? SpeechRecognizer.createOnDeviceSpeechRecognizer(getContext())
                    : SpeechRecognizer.createSpeechRecognizer(getContext());
            Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, call.getString("language", "en-IN"))
                    .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
                    .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
            sr.setRecognitionListener(new RecognitionListener() {
                private void done(String text, String err) {
                    JSObject r = new JSObject();
                    r.put("text", text == null ? "" : text);
                    if (err != null) r.put("error", err);
                    call.resolve(r);
                    sr.destroy();
                }
                @Override public void onResults(Bundle b) {
                    ArrayList<String> m = b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                    done(m != null && !m.isEmpty() ? m.get(0) : "", null);
                }
                @Override public void onError(int error) { done("", "speech error " + error); }
                @Override public void onReadyForSpeech(Bundle b) { notifyListeners("listening", new JSObject()); }
                @Override public void onRmsChanged(float rms) {
                    JSObject o = new JSObject(); o.put("level", Math.max(0, rms) / 10.0); notifyListeners("level", o);
                }
                @Override public void onBeginningOfSpeech() { }
                @Override public void onBufferReceived(byte[] buffer) { }
                @Override public void onEndOfSpeech() { }
                @Override public void onPartialResults(Bundle b) { }
                @Override public void onEvent(int t, Bundle b) { }
            });
            sr.startListening(intent);
        });
    }

    @PermissionCallback
    private void listenPermCallback(PluginCall call) {
        if (getPermissionState("microphone") == PermissionState.GRANTED) listen(call);
        else call.reject("Microphone permission denied");
    }

    // ------------------------------------------------------------------ text to speech (offline Android TTS)
    @PluginMethod
    public void speak(PluginCall call) {
        String text = call.getString("text", "");
        if (!ttsReady || text.isEmpty()) { call.resolve(); return; }
        String id = "u" + System.nanoTime();
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String u) { }
            @Override public void onDone(String u) { if (id.equals(u)) { call.resolve(); wakeService(WakeWordService.ACTION_RESUME); } }
            @Override public void onError(String u) { if (id.equals(u)) { call.resolve(); wakeService(WakeWordService.ACTION_RESUME); } }
        });
        wakeService(WakeWordService.ACTION_PAUSE);
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, id);
    }

    @PluginMethod
    public void resumeWake(PluginCall call) {
        wakeService(WakeWordService.ACTION_RESUME);
        call.resolve();
    }

    // ------------------------------------------------------------------ biometric confirmation for high-risk approvals
    @PluginMethod
    public void biometric(PluginCall call) {
        getActivity().runOnUiThread(() -> {
            int auth = Build.VERSION.SDK_INT >= 30
                    ? BiometricManager.Authenticators.BIOMETRIC_STRONG | BiometricManager.Authenticators.DEVICE_CREDENTIAL
                    : BiometricManager.Authenticators.BIOMETRIC_WEAK;
            BiometricPrompt.PromptInfo.Builder b = new BiometricPrompt.PromptInfo.Builder()
                    .setTitle("Confirm with JARVIS")
                    .setSubtitle(call.getString("reason", "Confirm this action"))
                    .setAllowedAuthenticators(auth);
            if (Build.VERSION.SDK_INT < 30) b.setNegativeButtonText("Cancel");
            Executor ex = ContextCompat.getMainExecutor(getContext());
            new BiometricPrompt(getActivity(), ex, new BiometricPrompt.AuthenticationCallback() {
                @Override public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult r) {
                    JSObject o = new JSObject(); o.put("verified", true); call.resolve(o);
                }
                @Override public void onAuthenticationError(int code, CharSequence msg) {
                    JSObject o = new JSObject(); o.put("verified", false); o.put("error", msg.toString()); call.resolve(o);
                }
            }).authenticate(b.build());
        });
    }

    // ------------------------------------------------------------------ secure storage (Android Keystore-backed)
    private SharedPreferences secure() throws Exception {
        MasterKey key = new MasterKey.Builder(getContext()).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build();
        return EncryptedSharedPreferences.create(getContext(), "jarvis_secure", key,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM);
    }

    @PluginMethod
    public void secureSet(PluginCall call) {
        try {
            SharedPreferences.Editor e = secure().edit();
            String v = call.getString("value");
            if (v == null) e.remove(call.getString("key")); else e.putString(call.getString("key"), v);
            e.apply();
            call.resolve();
        } catch (Exception ex) { call.reject("secure storage unavailable", ex); }
    }

    @PluginMethod
    public void secureGet(PluginCall call) {
        try {
            JSObject o = new JSObject();
            o.put("value", secure().getString(call.getString("key"), null));
            call.resolve(o);
        } catch (Exception ex) { call.reject("secure storage unavailable", ex); }
    }

    // ------------------------------------------------------------------ device actions
    @PluginMethod
    public void device(PluginCall call) {
        String cmd = call.getString("command", "");
        JSObject args = call.getObject("args", new JSObject());
        String alias = cmd.equals("find_contact") ? "contacts" : cmd.equals("get_location") ? "location" : null;
        if (alias != null && getPermissionState(alias) != PermissionState.GRANTED) {
            requestPermissionForAlias(alias, call, "devicePermCallback");
            return;
        }
        try {
            call.resolve(run(cmd, args));
        } catch (Exception e) {
            call.resolve(err(e.getMessage() == null ? e.toString() : e.getMessage()));
        }
    }

    @PermissionCallback
    private void devicePermCallback(PluginCall call) {
        String cmd = call.getString("command", "");
        String alias = cmd.equals("find_contact") ? "contacts" : "location";
        if (getPermissionState(alias) != PermissionState.GRANTED) {
            call.resolve(err("Android permission was denied. You can allow it in Settings > Apps > JARVIS."));
            return;
        }
        device(call);
    }

    private static JSObject ok() { JSObject o = new JSObject(); o.put("ok", true); return o; }
    private static JSObject err(String m) { JSObject o = new JSObject(); o.put("ok", false); o.put("error", m); return o; }

    private void startActivity(Intent i) {
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        getContext().startActivity(i);
    }

    private JSObject run(String cmd, JSObject a) throws Exception {
        Context c = getContext();
        switch (cmd) {
            case "set_alarm": {
                startActivity(new Intent(AlarmClock.ACTION_SET_ALARM)
                        .putExtra(AlarmClock.EXTRA_HOUR, a.getInt("hour"))
                        .putExtra(AlarmClock.EXTRA_MINUTES, a.getInt("minute"))
                        .putExtra(AlarmClock.EXTRA_MESSAGE, a.optString("label", "JARVIS"))
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true));
                return ok();
            }
            case "set_timer": {
                startActivity(new Intent(AlarmClock.ACTION_SET_TIMER)
                        .putExtra(AlarmClock.EXTRA_LENGTH, a.getInt("seconds"))
                        .putExtra(AlarmClock.EXTRA_MESSAGE, a.optString("label", "Timer"))
                        .putExtra(AlarmClock.EXTRA_SKIP_UI, true));
                return ok();
            }
            case "set_reminder": {
                long at = OffsetDateTime.parse(a.getString("at_iso")).toInstant().toEpochMilli();
                Intent i = new Intent(c, ReminderReceiver.class).putExtra("label", a.optString("label", "Reminder"));
                PendingIntent pi = PendingIntent.getBroadcast(c, (int) (at / 1000 % Integer.MAX_VALUE), i,
                        PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
                AlarmManager am = c.getSystemService(AlarmManager.class);
                if (Build.VERSION.SDK_INT >= 31 && !am.canScheduleExactAlarms()) am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
                else am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
                return ok();
            }
            case "open_app": {
                String q = a.optString("app", "");
                if (BLOCKED_APPS.matcher(q).find()) return err("blocked app");
                PackageManager pm = c.getPackageManager();
                Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
                List<ResolveInfo> apps = pm.queryIntentActivities(main, 0);
                for (ResolveInfo ri : apps) {
                    String label = ri.loadLabel(pm).toString();
                    if (label.toLowerCase(Locale.ROOT).contains(q.toLowerCase(Locale.ROOT))) {
                        if (BLOCKED_APPS.matcher(label).find()) return err("blocked app");
                        Intent launch = pm.getLaunchIntentForPackage(ri.activityInfo.packageName);
                        if (launch == null) continue;
                        startActivity(launch);
                        JSObject o = ok(); o.put("app", label); return o;
                    }
                }
                return err(q + " is not installed");
            }
            case "call": {
                startActivity(new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(a.getString("number")))));
                JSObject o = ok(); o.put("note", "Dialer opened; you press call."); return o;
            }
            case "send_sms": {
                // Opens Messages with the approved text filled in; you press send. No SMS permission needed.
                startActivity(new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(a.getString("number"))))
                        .putExtra("sms_body", a.getString("text")));
                JSObject o = ok(); o.put("note", "Messages opened with your text."); return o;
            }
            case "find_contact": return findContact(a.optString("name", ""));
            case "get_location": {
                LocationManager lm = c.getSystemService(LocationManager.class);
                Location best = null;
                for (String p : lm.getProviders(true)) {
                    @SuppressWarnings("MissingPermission") Location l = lm.getLastKnownLocation(p);
                    if (l != null && (best == null || l.getTime() > best.getTime())) best = l;
                }
                if (best == null) return err("No recent location. Open Maps once and try again.");
                JSObject o = ok(); o.put("lat", best.getLatitude()); o.put("lon", best.getLongitude()); o.put("name", "your location");
                return o;
            }
            case "read_notifications": {
                if (!JarvisNotificationListener.isEnabled(c)) {
                    startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
                    return err("Turn on notification access for JARVIS in the screen I just opened.");
                }
                JSObject o = ok();
                o.put("notifications", JarvisNotificationListener.recent(a.optInt("limit", 10)));
                return o;
            }
            case "toggle": return toggle(a.getString("setting"), a.getBoolean("state"));
            case "battery": {
                BatteryManager bm = c.getSystemService(BatteryManager.class);
                JSObject o = ok();
                o.put("level", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY));
                o.put("charging", bm.isCharging());
                return o;
            }
        }
        return err("unknown command " + cmd);
    }

    private JSObject findContact(String name) {
        JSArray list = new JSArray();
        String[] proj = {ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER};
        try (Cursor cur = getContext().getContentResolver().query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI, proj,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " LIKE ?", new String[]{"%" + name + "%"}, null)) {
            while (cur != null && cur.moveToNext() && list.length() < 5) {
                JSObject o = new JSObject();
                o.put("name", cur.getString(1));
                o.put("phone", cur.getString(2));
                o.put("email", email(cur.getString(0)));
                list.put(o);
            }
        }
        JSObject o = ok(); o.put("contacts", list); return o;
    }

    private String email(String contactId) {
        try (Cursor cur = getContext().getContentResolver().query(ContactsContract.CommonDataKinds.Email.CONTENT_URI,
                new String[]{ContactsContract.CommonDataKinds.Email.ADDRESS},
                ContactsContract.CommonDataKinds.Email.CONTACT_ID + " = ?", new String[]{contactId}, null)) {
            return cur != null && cur.moveToFirst() ? cur.getString(0) : "";
        }
    }

    private JSObject toggle(String setting, boolean on) throws Exception {
        Context c = getContext();
        JSObject o = ok();
        o.put("setting", setting);
        o.put("state", on);
        switch (setting) {
            case "flashlight": {
                CameraManager cm = c.getSystemService(CameraManager.class);
                for (String id : cm.getCameraIdList()) {
                    Boolean has = cm.getCameraCharacteristics(id).get(android.hardware.camera2.CameraCharacteristics.FLASH_INFO_AVAILABLE);
                    if (Boolean.TRUE.equals(has)) { cm.setTorchMode(id, on); return o; }
                }
                return err("No flashlight on this phone");
            }
            case "dnd": case "silent": {
                NotificationManager nm = c.getSystemService(NotificationManager.class);
                if (!nm.isNotificationPolicyAccessGranted()) {
                    startActivity(new Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS));
                    return err("Allow Do Not Disturb access for JARVIS in the screen I just opened.");
                }
                if (setting.equals("dnd")) nm.setInterruptionFilter(on ? NotificationManager.INTERRUPTION_FILTER_PRIORITY : NotificationManager.INTERRUPTION_FILTER_ALL);
                else c.getSystemService(AudioManager.class).setRingerMode(on ? AudioManager.RINGER_MODE_SILENT : AudioManager.RINGER_MODE_NORMAL);
                return o;
            }
            case "wifi": {
                // Since Android 10 apps cannot switch Wi-Fi directly; the system panel is the allowed way.
                startActivity(new Intent(Build.VERSION.SDK_INT >= 29 ? Settings.Panel.ACTION_WIFI : Settings.ACTION_WIFI_SETTINGS));
                o.put("note", "Opened the Wi-Fi panel; tap the switch.");
                return o;
            }
            case "bluetooth": {
                startActivity(new Intent(Settings.ACTION_BLUETOOTH_SETTINGS));
                o.put("note", "Opened Bluetooth settings; tap the switch.");
                return o;
            }
        }
        return err("can't change " + setting);
    }
}
