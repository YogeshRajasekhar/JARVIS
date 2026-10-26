package in.yogesh.jarvis;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

/**
 * Foreground service that listens for "hey jarvis" while the user allows it.
 * - Visible, persistent notification (Android requires it; it also keeps the user informed).
 * - Audio is processed on-device by WakeWordEngine and discarded; nothing is recorded or uploaded.
 * - Pauses itself while JARVIS is listening/speaking (the app sends ACTION_PAUSE / ACTION_RESUME).
 */
public class WakeWordService extends Service {
    public static final String ACTION_START = "in.yogesh.jarvis.WAKE_START";
    public static final String ACTION_STOP = "in.yogesh.jarvis.WAKE_STOP";
    public static final String ACTION_PAUSE = "in.yogesh.jarvis.WAKE_PAUSE";
    public static final String ACTION_RESUME = "in.yogesh.jarvis.WAKE_RESUME";
    public static final String EVENT_WAKE = "in.yogesh.jarvis.WAKE_DETECTED";
    private static final String CH_LISTEN = "jarvis_listen";
    private static final String CH_WAKE = "jarvis_wake";
    private static final float THRESHOLD = 0.5f;
    private static final long REFRACTORY_MS = 2000;

    private volatile boolean running = false;
    private static volatile boolean active = false;

    /** True while the listener is switched on, so pause/resume aren't sent to a stopped service. */
    public static boolean isActive() { return active; }
    private volatile boolean paused = false;
    private Thread worker;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String a = intent != null ? intent.getAction() : ACTION_START;
        if (ACTION_STOP.equals(a)) { stopListening(); stopSelf(); return START_NOT_STICKY; }
        if (ACTION_PAUSE.equals(a)) { paused = true; return START_STICKY; }
        if (ACTION_RESUME.equals(a)) { paused = false; return START_STICKY; }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            stopSelf();
            return START_NOT_STICKY;
        }
        channels();
        Notification n = new NotificationCompat.Builder(this, CH_LISTEN)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("JARVIS is listening for \"Hey JARVIS\"")
                .setContentText("Audio stays on this phone. Tap to open.")
                .setOngoing(true)
                .setContentIntent(openApp(false))
                .addAction(0, "Stop", PendingIntent.getService(this, 1,
                        new Intent(this, WakeWordService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE))
                .build();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            startForeground(7, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        } else {
            startForeground(7, n);
        }
        if (!running) startListening();
        return START_STICKY;
    }

    private PendingIntent openApp(boolean wake) {
        Intent i = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (wake) i.putExtra("jarvis_wake", true);
        return PendingIntent.getActivity(this, wake ? 3 : 2, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private void channels() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(new NotificationChannel(CH_LISTEN, "Wake word", NotificationManager.IMPORTANCE_LOW));
            nm.createNotificationChannel(new NotificationChannel(CH_WAKE, "JARVIS activation", NotificationManager.IMPORTANCE_HIGH));
        }
    }

    private void startListening() {
        running = true;
        active = true;
        worker = new Thread(() -> {
            int min = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            AudioRecord rec;
            try {
                rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, 16000, AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT, Math.max(min, WakeWordEngine.FRAME * 4));
            } catch (SecurityException e) { running = false; return; }
            try (WakeWordEngine engine = new WakeWordEngine(this)) {
                short[] frame = new short[WakeWordEngine.FRAME];
                long last = 0;
                boolean recording = false;
                while (running) {
                    if (paused) { // release the mic so speech recognition can use it
                        if (recording) { rec.stop(); recording = false; }
                        SystemClock.sleep(100);
                        continue;
                    }
                    if (!recording) { rec.startRecording(); recording = true; engine.reset(); }
                    int got = 0;
                    while (got < frame.length && running) {
                        int n = rec.read(frame, got, frame.length - got);
                        if (n <= 0) break;
                        got += n;
                    }
                    if (got < frame.length) continue;
                    float score = engine.process(frame);
                    long now = SystemClock.elapsedRealtime();
                    if (score >= THRESHOLD && now - last > REFRACTORY_MS) {
                        last = now;
                        engine.reset();
                        onWake();
                    }
                }
            } catch (Exception e) {
                Log.e("JarvisWake", "wake word stopped", e);
            } finally {
                try { rec.stop(); } catch (Exception ignored) { }
                rec.release();
                running = false;
            }
        }, "jarvis-wake");
        worker.start();
    }

    private void onWake() {
        paused = true; // the app resumes us after the conversation turn
        if (JarvisPlugin.isAppVisible()) {
            sendBroadcast(new Intent(EVENT_WAKE).setPackage(getPackageName()));
        } else {
            // Android blocks background activity starts; a full-screen notification is the allowed way.
            Notification n = new NotificationCompat.Builder(this, CH_WAKE)
                    .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                    .setContentTitle("JARVIS")
                    .setContentText("Listening. Tap if the screen didn't open.")
                    .setPriority(NotificationCompat.PRIORITY_HIGH)
                    .setCategory(NotificationCompat.CATEGORY_CALL)
                    .setAutoCancel(true)
                    .setFullScreenIntent(openApp(true), true)
                    .setContentIntent(openApp(true))
                    .build();
            getSystemService(NotificationManager.class).notify(8, n);
        }
    }

    private void stopListening() {
        running = false;
        active = false;
        if (worker != null) worker.interrupt();
    }

    @Override
    public void onDestroy() {
        stopListening();
        super.onDestroy();
    }
}
