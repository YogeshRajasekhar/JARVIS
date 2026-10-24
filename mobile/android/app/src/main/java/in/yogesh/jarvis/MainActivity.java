package in.yogesh.jarvis;

import android.os.Bundle;
import android.view.WindowManager;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(JarvisPlugin.class);
        super.onCreate(savedInstanceState);
        // Approval sheets and the conversation must not leak into screenshots or the recent-apps preview.
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
    }
}
