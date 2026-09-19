package org.lanmanager.prototype;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Typeface;
import android.os.SystemClock;
import android.util.AttributeSet;
import android.util.TypedValue;
import android.view.MotionEvent;
import android.view.View;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/** Draws only sessions observed by this device; geometry has no routing meaning. */
public final class ObservedTopologyView extends View {
    public interface Listener { void on_peer_selected(PeerSnapshot peer); }

    private static final class Node {
        final PeerSnapshot peer;
        final float x;
        final float y;
        Node(PeerSnapshot peer, float x, float y) {
            this.peer = peer;
            this.x = x;
            this.y = y;
        }
    }

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final List<PeerSnapshot> peers = new ArrayList<>();
    private final List<Node> nodes = new ArrayList<>();
    private String local_name = "Android";
    private Listener listener;

    public ObservedTopologyView(Context context, AttributeSet attrs) {
        super(context, attrs);
        setFocusable(false);
        setClickable(true);
    }

    public void set_peers(String local_name, List<PeerSnapshot> peers) {
        this.local_name = local_name;
        this.peers.clear();
        this.peers.addAll(peers);
        rebuild_nodes();
        setContentDescription(getResources().getQuantityString(
            R.plurals.observed_sessions, peers.size(), peers.size()));
        invalidate();
    }

    public void set_listener(Listener listener) {
        this.listener = listener;
    }

    @Override protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        float cx = getWidth() / 2f;
        float cy = getHeight() / 2f;
        int border = resolve(R.attr.lanBorder);
        int accent = resolve(R.attr.lanAccent);
        int signal = resolve(R.attr.lanSignal);
        int surface = resolve(R.attr.lanRaised);
        int primary = resolve(R.attr.lanPrimaryText);
        int secondary = resolve(R.attr.lanSecondaryText);

        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(1.5f));
        paint.setColor(border);
        for (Node node : nodes) {
            canvas.drawLine(cx, cy, node.x, node.y, paint);
        }
        draw_node(canvas, cx, cy, dp(43), accent, signal, primary,
                  local_name, getContext().getString(R.string.this_device));
        for (Node node : nodes) {
            double age = Math.max(0, SystemClock.elapsedRealtime() - node.peer.last_seen_ms) / 1000.0;
            draw_node(canvas, node.x, node.y, dp(37), surface, border, primary,
                      ellipsize(node.peer.name, 15),
                      String.format(Locale.getDefault(), "%.1fs", age));
        }
        if (peers.isEmpty()) {
            paint.setStyle(Paint.Style.FILL);
            paint.setColor(secondary);
            paint.setTextSize(sp(13));
            paint.setTextAlign(Paint.Align.CENTER);
            canvas.drawText(getContext().getString(R.string.waiting_devices),
                            cx, cy + dp(72), paint);
        }
    }

    private void draw_node(Canvas canvas, float x, float y, float radius, int fill,
                           int stroke, int text, String title, String detail) {
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(fill);
        canvas.drawCircle(x, y, radius, paint);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(dp(2));
        paint.setColor(stroke);
        canvas.drawCircle(x, y, radius, paint);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(text);
        paint.setTextAlign(Paint.Align.CENTER);
        paint.setTypeface(Typeface.DEFAULT_BOLD);
        paint.setTextSize(sp(12));
        canvas.drawText(title, x, y - dp(2), paint);
        paint.setTypeface(Typeface.DEFAULT);
        paint.setTextSize(sp(10));
        canvas.drawText(detail, x, y + dp(15), paint);
    }

    @Override public boolean onTouchEvent(MotionEvent event) {
        if (event.getAction() != MotionEvent.ACTION_UP) return true;
        for (Node node : nodes) {
            if (Math.hypot(event.getX() - node.x, event.getY() - node.y) <= dp(45)) {
                performClick();
                if (listener != null) listener.on_peer_selected(node.peer);
                return true;
            }
        }
        return super.onTouchEvent(event);
    }

    @Override public boolean performClick() {
        super.performClick();
        return true;
    }

    @Override protected void onSizeChanged(int width, int height,
                                           int old_width, int old_height) {
        super.onSizeChanged(width, height, old_width, old_height);
        rebuild_nodes();
    }

    private void rebuild_nodes() {
        nodes.clear();
        float cx = getWidth() / 2f;
        float cy = getHeight() / 2f;
        float radius = Math.min(getWidth(), getHeight()) * 0.31f;
        for (int index = 0; index < peers.size(); index++) {
            double angle = -Math.PI / 2
                + (2 * Math.PI * index / Math.max(peers.size(), 1));
            float x = cx + (float) Math.cos(angle) * radius;
            float y = cy + (float) Math.sin(angle) * radius;
            nodes.add(new Node(peers.get(index), x, y));
        }
    }

    private int resolve(int attribute) {
        TypedValue value = new TypedValue();
        getContext().getTheme().resolveAttribute(attribute, value, true);
        return value.data;
    }

    private float dp(float value) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                                         getResources().getDisplayMetrics());
    }

    private float sp(float value) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, value,
                                         getResources().getDisplayMetrics());
    }

    private static String ellipsize(String value, int limit) {
        if (value.codePointCount(0, value.length()) <= limit) return value;
        int end = value.offsetByCodePoints(0, limit - 1);
        return value.substring(0, end) + "…";
    }
}
