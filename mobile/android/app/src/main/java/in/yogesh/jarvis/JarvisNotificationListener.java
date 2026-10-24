package in.yogesh.jarvis;

import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.provider.Settings;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;

import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.Iterator;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * Keeps the last 30 notifications IN MEMORY ONLY (nothing written to disk) so JARVIS can brief you.
 * Banking/payment/password apps and anything that looks like an OTP are dropped on arrival,
 * so they never reach the assistant, the server or an LLM.
 */
public class JarvisNotificationListener extends NotificationListenerService {
    private static final ArrayDeque<JSObject> RECENT = new ArrayDeque<>();
    private static final Pattern BLOCKED = Pattern.compile(
            "pay|bank|sbi|hdfc|icici|axis|kotak|paytm|phonepe|wallet|zerodha|groww|password|authenticator|aadhaar|upi",
            Pattern.CASE_INSENSITIVE);
    private static final Pattern OTP = Pattern.compile("(?i)\\b(otp|one[- ]time|verification code|passcode|security code)\\b|\\b\\d{4,8}\\b.*\\bcode\\b");

    public static boolean isEnabled(Context c) {
        String flat = Settings.Secure.getString(c.getContentResolver(), "enabled_notification_listeners");
        return flat != null && flat.contains(new ComponentName(c, JarvisNotificationListener.class).flattenToString());
    }

    public static synchronized JSArray recent(int limit) {
        JSArray a = new JSArray();
        Iterator<JSObject> it = RECENT.descendingIterator();
        while (it.hasNext() && a.length() < limit) a.put(it.next());
        return a;
    }

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn.isOngoing() || sbn.getPackageName().equals(getPackageName())) return;
        String app;
        try {
            PackageManager pm = getPackageManager();
            app = pm.getApplicationLabel(pm.getApplicationInfo(sbn.getPackageName(), 0)).toString();
        } catch (Exception e) { app = sbn.getPackageName(); }
        if (BLOCKED.matcher(app).find() || BLOCKED.matcher(sbn.getPackageName()).find()) return;
        Bundle ex = sbn.getNotification().extras;
        String title = String.valueOf(ex.getCharSequence("android.title", ""));
        String text = String.valueOf(ex.getCharSequence("android.text", ""));
        if (OTP.matcher(title + " " + text).find()) return;
        JSObject o = new JSObject();
        o.put("app", app);
        o.put("title", title);
        o.put("text", text.length() > 200 ? text.substring(0, 200) : text);
        o.put("time", new SimpleDateFormat("HH:mm", Locale.ROOT).format(new Date(sbn.getPostTime())));
        synchronized (JarvisNotificationListener.class) {
            RECENT.addLast(o);
            while (RECENT.size() > 30) RECENT.removeFirst();
        }
    }
}
