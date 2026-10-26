package in.yogesh.jarvis;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;

import androidx.core.app.NotificationCompat;

/**
 * Fires a JARVIS reminder at the time you asked for. Reminders you asked JARVIS to CALL you about
 * ring like an incoming call (full screen, ringtone); answering opens JARVIS straight into a voice call.
 */
public class ReminderReceiver extends BroadcastReceiver {
    static final String DECLINE = "in.yogesh.jarvis.CALL_DECLINE";
    static final int CALL_NOTE = 71;

    @Override
    public void onReceive(Context c, Intent intent) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (DECLINE.equals(intent.getAction())) { nm.cancel(CALL_NOTE); return; }
        String label = intent.getStringExtra("label");
        if (label == null) label = "Reminder";
        if (intent.getBooleanExtra("call", false)) { ring(c, nm, label); return; }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(new NotificationChannel("jarvis_reminders", "Reminders", NotificationManager.IMPORTANCE_HIGH));
        }
        PendingIntent open = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify((int) (System.currentTimeMillis() % 100000),
                new NotificationCompat.Builder(c, "jarvis_reminders")
                        .setSmallIcon(android.R.drawable.ic_popup_reminder)
                        .setContentTitle("Reminder")
                        .setContentText(label)
                        .setPriority(NotificationCompat.PRIORITY_HIGH)
                        .setAutoCancel(true)
                        .setContentIntent(open)
                        .build());
    }

    static void ring(Context c, NotificationManager nm, String reason) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel ch = new NotificationChannel("jarvis_calls", "Calls from JARVIS", NotificationManager.IMPORTANCE_HIGH);
            ch.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
                    new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_RINGTONE).build());
            ch.enableVibration(true);
            ch.setVibrationPattern(new long[]{0, 800, 600, 800, 600, 800});
            nm.createNotificationChannel(ch);
        }
        Intent answer = new Intent(c, MainActivity.class).putExtra("jarvis_call", reason)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent answerPi = PendingIntent.getActivity(c, 710, answer, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent declinePi = PendingIntent.getBroadcast(c, 711, new Intent(c, ReminderReceiver.class).setAction(DECLINE),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        nm.notify(CALL_NOTE, new NotificationCompat.Builder(c, "jarvis_calls")
                .setSmallIcon(android.R.drawable.sym_call_incoming)
                .setContentTitle("JARVIS is calling")
                .setContentText(reason)
                .setPriority(NotificationCompat.PRIORITY_MAX)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setOngoing(true)
                .setAutoCancel(true)
                .setTimeoutAfter(60_000)
                .setFullScreenIntent(answerPi, true)
                .setContentIntent(answerPi)
                .addAction(0, "Decline", declinePi)
                .addAction(0, "Answer", answerPi)
                .build());
    }
}
