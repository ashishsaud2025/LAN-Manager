package org.lanmanager.prototype;

import android.app.Activity;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.JsonReader;
import android.util.JsonToken;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Foreground discovery and bidirectional echo experiment, without a cloud service. */
public class MainActivity extends Activity {
    private EditText name_input, host_input;
    private TextView status, roster_view, log_view;
    private final StringBuilder log_buffer = new StringBuilder();
    private String peer_id;
    private volatile Session current;

    private static final class Session {
        final String id = UUID.randomUUID().toString();
        volatile boolean running = true;
        volatile DatagramSocket udp;
        volatile ServerSocket listener;
        WifiManager.MulticastLock wifi_lock;
        final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
        final ThreadPoolExecutor workers = new ThreadPoolExecutor(4, 4, 0,
            TimeUnit.SECONDS, new ArrayBlockingQueue<>(4));
    }

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        peer_id = getPreferences(MODE_PRIVATE).getString("peer_id", null);
        if (peer_id == null) {
            peer_id = UUID.randomUUID().toString();
            getPreferences(MODE_PRIVATE).edit().putString("peer_id", peer_id).apply();
        }
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(24, 48, 24, 24);
        ScrollView scroll = new ScrollView(this);
        scroll.addView(layout);
        setContentView(scroll);
        status = new TextView(this);
        status.setText("Foreground testing only. UDP 50000; TCP echo 50002.\nPeer: " + peer_id);
        layout.addView(status);
        name_input = new EditText(this);
        name_input.setSingleLine(true);
        name_input.setText("Android Phone");
        layout.addView(name_input);
        add_button(layout, "Start discovery + echo server", view -> start_session());
        add_button(layout, "Stop", view -> stop_session());
        host_input = new EditText(this);
        host_input.setSingleLine(true);
        host_input.setHint("Desktop LAN IPv4 address for echo test");
        layout.addView(host_input);
        add_button(layout, "Probe desktop TCP 50002", view -> probe());
        roster_view = new TextView(this);
        roster_view.setText("No active roster");
        layout.addView(roster_view);
        log_view = new TextView(this);
        layout.addView(log_view);
    }

    private void add_button(LinearLayout layout, String label, View.OnClickListener action) {
        Button button = new Button(this);
        button.setText(label);
        button.setOnClickListener(action);
        layout.addView(button);
    }

    private void report(Session session, String text) {
        Log.i("LANManager", text);
        runOnUiThread(() -> {
            if (session != null && current != session) return;
            log_buffer.append(text).append('\n');
            if (log_buffer.length() > 12000) log_buffer.delete(0, log_buffer.length() - 8000);
            log_view.setText(log_buffer.toString());
        });
    }

    private void start_session() {
        stop_session();
        String name = name_input.getText().toString().trim();
        if (name.isEmpty() || name.codePointCount(0, name.length()) > 80) {
            report(null, "Name must contain 1 to 80 characters");
            return;
        }
        Session session = new Session();
        current = session;
        WifiManager manager = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        if (manager != null) {
            session.wifi_lock = manager.createMulticastLock("lan-manager-test");
            session.wifi_lock.setReferenceCounted(false);
            session.wifi_lock.acquire();
        }
        report(session, "Started session " + session.id);
        session.workers.execute(() -> discovery(session, name));
        session.workers.execute(() -> echo_server(session));
    }

    private void stop_session() {
        Session session = current;
        if (session == null) return;
        current = null;
        session.running = false;
        if (session.udp != null) session.udp.close();
        try { if (session.listener != null) session.listener.close(); }
        catch (IOException error) { Log.d("LANManager", "Listener close", error); }
        for (Socket socket : session.sockets) {
            try { socket.close(); }
            catch (IOException error) { Log.d("LANManager", "Socket close", error); }
        }
        session.workers.shutdownNow();
        if (session.wifi_lock != null && session.wifi_lock.isHeld()) session.wifi_lock.release();
        roster_view.setText("Stopped; press Start to create a new session");
        report(null, "Networking stopped");
    }

    @Override protected void onStop() {
        stop_session();
        super.onStop();
    }

    private void discovery(Session session, String name) {
        Map<String, Long> seen = new HashMap<>();
        Map<String, String> peers = new HashMap<>();
        try (DatagramSocket socket = new DatagramSocket(null)) {
            session.udp = socket;
            socket.setReuseAddress(true);
            socket.setBroadcast(true);
            socket.bind(new InetSocketAddress("0.0.0.0", 50000));
            socket.setSoTimeout(250);
            JSONObject hello = new JSONObject().put("version", 1).put("peer_id", peer_id)
                .put("session_id", session.id).put("name", name).put("tcp_port", 50002)
                .put("capabilities", new JSONArray().put("echo_v1"));
            byte[] packet = ("LMAN\u0001" + hello).getBytes(StandardCharsets.UTF_8);
            if (packet.length > 1200) throw new IOException("HELLO too large");
            long next = 0;
            String last_roster = "";
            while (session.running) {
                long now = SystemClock.elapsedRealtime();
                if (now >= next) {
                    socket.send(new DatagramPacket(packet, packet.length,
                        InetAddress.getByName("255.255.255.255"), 50000));
                    next = now + 2000;
                }
                DatagramPacket incoming = new DatagramPacket(new byte[65535], 65535);
                try {
                    socket.receive(incoming);
                    int size = incoming.getLength();
                    byte[] data = incoming.getData();
                    if (size < 6 || size > 1200 || data[0] != 'L' || data[1] != 'M'
                            || data[2] != 'A' || data[3] != 'N' || data[4] != 1) continue;
                    JSONObject other = parse(new String(data, 5, size - 5, StandardCharsets.UTF_8));
                    if (!Integer.valueOf(1).equals(other.get("version"))) continue;
                    String id = canonical_uuid(other.getString("session_id"));
                    canonical_uuid(other.getString("peer_id"));
                    if (id.equals(session.id)) continue;
                    int port = other.getInt("tcp_port");
                    if (port < 1 || port > 65535) continue;
                    String label = other.getString("name");
                    if (label.isBlank() || label.codePointCount(0, label.length()) > 80) continue;
                    other.getJSONArray("capabilities");
                    if (!seen.containsKey(id) && seen.size() >= 256) continue;
                    seen.put(id, SystemClock.elapsedRealtime());
                    peers.put(id, label + " @ " + incoming.getAddress().getHostAddress() + ":" + port);
                } catch (SocketTimeoutException ignored) {
                    // Receive silence must still allow heartbeat and expiry checks.
                } catch (JSONException | IllegalArgumentException error) {
                    Log.w("LANManager", "Malformed HELLO", error);
                }
                long cutoff = SystemClock.elapsedRealtime() - 6000;
                seen.entrySet().removeIf(entry -> entry.getValue() <= cutoff);
                peers.keySet().retainAll(seen.keySet());
                String text = "Online sessions: " + peers.size() + "\n" + String.join("\n", peers.values());
                if (!text.equals(last_roster)) {
                    last_roster = text;
                    runOnUiThread(() -> { if (current == session) roster_view.setText(text); });
                }
            }
        } catch (Exception error) {
            if (session.running) report(session, "Discovery stopped: " + error);
        }
    }

    private JSONObject message(Session session, String type, JSONObject body) throws JSONException {
        return new JSONObject().put("version", 1).put("type", type)
            .put("message_id", UUID.randomUUID().toString()).put("peer_id", peer_id)
            .put("session_id", session.id).put("body", body);
    }

    private String canonical_uuid(String value) throws IOException {
        if (!UUID.fromString(value).toString().equals(value)) throw new IOException("Invalid UUID");
        return value;
    }

    private JSONObject receive(Socket socket) throws Exception {
        long deadline = SystemClock.elapsedRealtime() + 5000;
        InputStream stream = new FilterInputStream(socket.getInputStream()) {
            @Override public int read(byte[] bytes, int offset, int size) throws IOException {
                long remaining = deadline - SystemClock.elapsedRealtime();
                if (remaining <= 0) throw new SocketTimeoutException("Frame deadline");
                socket.setSoTimeout((int) remaining);
                return in.read(bytes, offset, size);
            }
        };
        String json = Frames.receive(stream);
        if (json == null) return null;
        JSONObject value = parse(json);
        if (!Integer.valueOf(1).equals(value.get("version"))) throw new IOException("Unsupported version");
        for (String key : new String[]{"peer_id", "session_id", "message_id"})
            canonical_uuid(value.getString(key));
        value.getJSONObject("body");
        return value;
    }

    private void echo_server(Session session) {
        try (ServerSocket listener = new ServerSocket()) {
            session.listener = listener;
            listener.setReuseAddress(true);
            listener.bind(new InetSocketAddress("0.0.0.0", 50002));
            listener.setSoTimeout(250);
            report(session, "TCP echo listening on 50002");
            while (session.running) {
                Socket socket;
                try { socket = listener.accept(); }
                catch (SocketTimeoutException ignored) { continue; }
                session.sockets.add(socket);
                try (socket) {
                    JSONObject request;
                    while (session.running && (request = receive(socket)) != null) {
                        if (!"ECHO".equals(request.getString("type"))) throw new IOException("Expected ECHO");
                        JSONObject reply = message(session, "ECHO_REPLY", request.getJSONObject("body"));
                        reply.put("reply_to", request.getString("message_id"));
                        Frames.send(socket.getOutputStream(), reply.toString());
                    }
                } catch (Exception error) {
                    if (session.running) report(session, "Echo connection ended: " + error);
                } finally { session.sockets.remove(socket); }
            }
        } catch (Exception error) {
            if (session.running) report(session, "Echo listener stopped: " + error);
        }
    }

    private void probe() {
        Session session = current;
        String host = host_input.getText().toString().trim();
        if (session == null || host.isEmpty()) { report(null, "Start networking and enter desktop IP"); return; }
        try {
            session.workers.execute(() -> {
                try (Socket socket = new Socket()) {
                    session.sockets.add(socket);
                    try {
                        socket.connect(new InetSocketAddress(host, 50002), 3000);
                        if (!session.running) return;
                        for (String text : new String[]{"hello", "नमस्ते", "x".repeat(100000), "end"}) {
                            JSONObject request = message(session, "ECHO", new JSONObject().put("text", text));
                            Frames.send(socket.getOutputStream(), request.toString());
                            JSONObject reply = receive(socket);
                            if (reply == null || !"ECHO_REPLY".equals(reply.getString("type"))
                                || !request.getString("message_id").equals(reply.getString("reply_to"))
                                || !text.equals(reply.getJSONObject("body").getString("text")))
                                throw new IOException("Echo mismatch");
                            report(session, "Verified echo: " + text.codePointCount(0, text.length()) + " characters");
                        }
                    } finally { session.sockets.remove(socket); }
                } catch (Exception error) { report(session, "Probe failed: " + error); }
            });
        } catch (java.util.concurrent.RejectedExecutionException error) { report(session, "Probe queue full"); }
    }

    private JSONObject parse(String json) throws IOException, JSONException {
        try (JsonReader reader = new JsonReader(new StringReader(json))) {
            Object value = read_json(reader, 0);
            if (!(value instanceof JSONObject) || reader.peek() != JsonToken.END_DOCUMENT)
                throw new IOException("Expected one JSON object");
            return (JSONObject) value;
        }
    }

    private Object read_json(JsonReader reader, int depth) throws IOException, JSONException {
        if (depth > 64) throw new IOException("JSON nesting limit");
        switch (reader.peek()) {
            case BEGIN_OBJECT:
                JSONObject object = new JSONObject();
                Set<String> keys = new HashSet<>();
                reader.beginObject();
                while (reader.hasNext()) {
                    String key = reader.nextName();
                    if (!keys.add(key)) throw new IOException("Duplicate JSON key");
                    object.put(key, read_json(reader, depth + 1));
                }
                reader.endObject();
                return object;
            case BEGIN_ARRAY:
                JSONArray array = new JSONArray();
                reader.beginArray();
                while (reader.hasNext()) array.put(read_json(reader, depth + 1));
                reader.endArray();
                return array;
            case STRING: return reader.nextString();
            case BOOLEAN: return reader.nextBoolean();
            case NULL: reader.nextNull(); return JSONObject.NULL;
            case NUMBER:
                String number = reader.nextString();
                try { return Integer.valueOf(number); }
                catch (NumberFormatException not_integer) {
                    double value = Double.parseDouble(number);
                    if (!Double.isFinite(value)) throw new IOException("Invalid JSON number");
                    return value;
                }
            default: throw new IOException("Invalid JSON token");
        }
    }
}
