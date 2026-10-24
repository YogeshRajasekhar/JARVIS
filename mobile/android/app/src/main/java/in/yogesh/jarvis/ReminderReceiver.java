package in.yogesh.jarvis;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;

/** Fires a JARVIS reminder notification at the time the user asked for. */
public class ReminderReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            nm.createNotificationChannel(new NotificationChannel("jarvis_reminders", "Reminders", NotificationManager.IMPORTANCE_HIGH));
        }
        PendingIntent open = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify((int) (System.currentTimeMillis() % 100000),
                new NotificationCompat.Builder(c, "jarvis_reminders")
                        .setSmallIcon(android.R.drawable.ic_popup_reminder)
                        .setContentTitle("Reminder")
                        .setContentText(intent.getStringExtra("label"))
                        .setPriority(NotificationCompat.PRIORITY_HIGH)
                        .setAutoCancel(true)
                        .setContentIntent(open)
                        .build());
    }
}
