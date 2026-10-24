package in.yogesh.jarvis;

import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtException;
import ai.onnxruntime.OrtSession;
import android.content.Context;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.FloatBuffer;
import java.util.ArrayDeque;
import java.util.Collections;

/**
 * On-device "hey jarvis" detector: a line-by-line port of the openWakeWord streaming pipeline
 * (verified against real audio by scripts/wakeword_reference.py, which runs the same steps).
 *
 * Per 80 ms frame (1280 samples @16 kHz):
 *   melspectrogram(last 1760 samples) -> 8 frames x 32 bins, transformed x/10 + 2
 *   keep the last 76 mel frames -> embedding model -> one 96-d vector
 *   keep the last 16 vectors -> hey_jarvis classifier -> score in [0, 1]
 * Audio never leaves the phone for wake-word detection.
 */
public class WakeWordEngine implements AutoCloseable {
    public static final int FRAME = 1280;
    private static final int MEL_CTX = 480;
    private static final int WINDOW = FRAME + MEL_CTX;

    private final OrtEnvironment env;
    private final OrtSession mel, emb, ww;
    private final short[] raw = new short[WINDOW];
    private float[][] melBuf = new float[76][32];
    private final ArrayDeque<float[]> feats = new ArrayDeque<>();
    private int frames = 0;

    public WakeWordEngine(Context ctx) throws IOException, OrtException {
        env = OrtEnvironment.getEnvironment();
        OrtSession.SessionOptions o = new OrtSession.SessionOptions();
        o.setIntraOpNumThreads(1);
        mel = env.createSession(asset(ctx, "models/melspectrogram.onnx"), o);
        emb = env.createSession(asset(ctx, "models/embedding_model.onnx"), o);
        ww = env.createSession(asset(ctx, "models/hey_jarvis_v0.1.onnx"), o);
        reset();
    }

    private static byte[] asset(Context ctx, String path) throws IOException {
        try (InputStream in = ctx.getAssets().open(path); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] b = new byte[16384];
            int n;
            while ((n = in.read(b)) > 0) out.write(b, 0, n);
            return out.toByteArray();
        }
    }

    public synchronized void reset() throws OrtException {
        java.util.Arrays.fill(raw, (short) 0);
        for (float[] row : melBuf) java.util.Arrays.fill(row, 1f);
        float[][] zeroMel = melspec(new short[76 * 160 + MEL_CTX]);
        float[][] last76 = new float[76][];
        System.arraycopy(zeroMel, zeroMel.length - 76, last76, 0, 76);
        float[] zeroEmb = embed(last76);
        feats.clear();
        for (int i = 0; i < 16; i++) feats.add(zeroEmb);
        frames = 0;
    }

    private float[][] melspec(short[] samples) throws OrtException {
        float[] f = new float[samples.length];
        for (int i = 0; i < samples.length; i++) f[i] = samples[i];
        try (OnnxTensor t = OnnxTensor.createTensor(env, FloatBuffer.wrap(f), new long[]{1, samples.length});
             OrtSession.Result r = mel.run(Collections.singletonMap("input", t))) {
            float[][][][] out = (float[][][][]) r.get(0).getValue(); // [1][1][frames][32]
            float[][] m = out[0][0];
            for (float[] row : m) for (int j = 0; j < row.length; j++) row[j] = row[j] / 10f + 2f;
            return m;
        }
    }

    private float[] embed(float[][] mel76) throws OrtException {
        float[] f = new float[76 * 32];
        for (int i = 0; i < 76; i++) System.arraycopy(mel76[i], 0, f, i * 32, 32);
        try (OnnxTensor t = OnnxTensor.createTensor(env, FloatBuffer.wrap(f), new long[]{1, 76, 32, 1});
             OrtSession.Result r = emb.run(Collections.singletonMap("input_1", t))) {
            float[][][][] out = (float[][][][]) r.get(0).getValue(); // [1][1][1][96]
            return out[0][0][0].clone();
        }
    }

    /** @param frame exactly FRAME samples. @return wake score for this frame. */
    public synchronized float process(short[] frame) throws OrtException {
        System.arraycopy(raw, FRAME, raw, 0, MEL_CTX);
        System.arraycopy(frame, 0, raw, MEL_CTX, FRAME);
        float[][] m = melspec(raw);
        float[][] merged = new float[76][];
        int keepOld = 76 - m.length;
        System.arraycopy(melBuf, melBuf.length - keepOld, merged, 0, keepOld);
        System.arraycopy(m, 0, merged, keepOld, m.length);
        melBuf = merged;
        feats.addLast(embed(melBuf));
        while (feats.size() > 16) feats.removeFirst();

        float[] f = new float[16 * 96];
        int i = 0;
        for (float[] v : feats) System.arraycopy(v, 0, f, 96 * i++, 96);
        float score;
        try (OnnxTensor t = OnnxTensor.createTensor(env, FloatBuffer.wrap(f), new long[]{1, 16, 96});
             OrtSession.Result r = ww.run(Collections.singletonMap(ww.getInputNames().iterator().next(), t))) {
            score = ((float[][]) r.get(0).getValue())[0][0];
        }
        frames++;
        return frames < 5 ? 0f : score;
    }

    @Override
    public void close() throws OrtException {
        mel.close();
        emb.close();
        ww.close();
    }
}
