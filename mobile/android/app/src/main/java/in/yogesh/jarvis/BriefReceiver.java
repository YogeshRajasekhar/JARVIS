package in.yogesh.jarvis;

import android.app.AlarmManager;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import androidx.core.app.NotificationCompat;

import java.util.Calendar;

/**
 * The morning call (reel: "You told me to call you at six with the overnight report. Now or in five minutes?").
 * Fires at the chosen time, shows an alarm-style notification with "Brief me" and "5 more minutes",
 * re-arms itself for the next day, and is restored after a reboot by BootReceiver.
 */
public class BriefReceiver extends BroadcastReceiver {
    static final String FIRE = "in.yogesh.jarvis.BRIEF_FIRE";
    static final String SNOOZE = "in.yogesh.jarvis.BRIEF_SNOOZE";
    private static final String CH = "jarvis_brief";
    private static final int NOTE_ID = 61;

    static SharedPreferences prefs(Context c) { return c.getSharedPreferences("jarvis_brief", Context.MODE_PRIVATE); }

    static void save(Context c, boolean enabled, int hour, int minute) {
        prefs(c).edit().putBoolean("enabled", enabled).putInt("hour", hour).putInt("minute", minute).apply();
        if (enabled) scheduleNext(c); else cancel(c);
    }

    private static PendingIntent fireIntent(Context c) {
        return PendingIntent.getBroadcast(c, 610, new Intent(c, BriefReceiver.class).setAction(FIRE),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static void cancel(Context c) {
        c.getSystemService(AlarmManager.class).cancel(fireIntent(c));
    }

    static void scheduleNext(Context c) {
        SharedPreferences p = prefs(c);
        if (!p.getBoolean("enabled", false)) return;
        Calendar at = Calendar.getInstance();
        at.set(Calendar.HOUR_OF_DAY, p.getInt("hour", 6));
        at.set(Calendar.MINUTE, p.getInt("minute", 0));
        at.set(Calendar.SECOND, 0);
        at.set(Calendar.MILLISECOND, 0);
        if (at.getTimeInMillis() <= System.currentTimeMillis() + 1000) at.add(Calendar.DAY_OF_YEAR, 1);
        scheduleAt(c, at.getTimeInMillis());
    }

    static void scheduleAt(Context c, long when) {
        AlarmManager am = c.getSystemService(AlarmManager.class);
        PendingIntent pi = fireIntent(c);
        if (Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms()) {
            PendingIntent show = PendingIntent.getActivity(c, 611, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
            am.setAlarmClock(new AlarmManager.AlarmClockInfo(when, show), pi);   // shows the alarm icon, survives Doze
        } else {
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, when, pi);
        }
    }

    @Override
    public void onReceive(Context c, Intent i) {
        String a = i.getAction();
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (SNOOZE.equals(a)) {
            nm.cancel(NOTE_ID);
            scheduleAt(c, System.currentTimeMillis() + 5 * 60 * 1000);
            return;
        }
        if (!FIRE.equals(a)) return;
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CH, "Morning call", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("Your daily overnight report");
            nm.createNotificationChannel(ch);
        }
        Intent open = new Intent(c, MainActivity.class).putExtra("jarvis_brief", true)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(c, 612, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        PendingIntent snoozePi = PendingIntent.getBroadcast(c, 613, new Intent(c, BriefReceiver.class).setAction(SNOOZE),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        nm.notify(NOTE_ID, new NotificationCompat.Builder(c, CH)
                .setSmallIcon(android.R.drawable.ic_lock_idle_alarm)
                .setContentTitle("Good morning. Your overnight report is ready.")
                .setContentText("Now, or in five minutes?")
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_ALARM)
                .setAutoCancel(true)
                .setContentIntent(openPi)
                .setFullScreenIntent(openPi, true)
                .addAction(0, "Brief me", openPi)
                .addAction(0, "5 more minutes", snoozePi)
                .build());
        scheduleNext(c);                                     // tomorrow
    }
}
