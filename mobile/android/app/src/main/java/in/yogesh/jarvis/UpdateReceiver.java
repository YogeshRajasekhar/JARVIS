package in.yogesh.jarvis;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageInstaller;

/** Receives the installer's result. Shows Android's "Update this app?" screen when it asks for confirmation. */
public class UpdateReceiver extends BroadcastReceiver {
    static volatile String lastResult = null;

    @Override
    public void onReceive(Context c, Intent i) {
        int status = i.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
        if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            @SuppressWarnings("deprecation")
            Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) {
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                c.startActivity(confirm);
            }
            lastResult = "waiting";
        } else if (status == PackageInstaller.STATUS_SUCCESS) {
            lastResult = "installed";
        } else {
            String msg = i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            lastResult = "failed: " + (msg == null ? ("status " + status) : msg);
        }
    }
}
