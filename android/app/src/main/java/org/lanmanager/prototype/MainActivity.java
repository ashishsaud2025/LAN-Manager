package org.lanmanager.prototype;

import android.app.Activity;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.JsonReader;
import android.util.JsonToken;
import android.util.Log;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
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
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Foreground-only Android network observatory with UDP discovery and framed ECHO. */
public class MainActivity extends Activity {
    private static final String TAG = "LANManager";
    private static final String THEME_PREFERENCES = "lan_atlas_ui";
    private static final String THEME_KEY = "theme";
    private static final DateTimeFormatter LOG_TIME = DateTimeFormatter.ofPattern("HH:mm:ss");
    private static final int PAGE_OVERVIEW = 0;
    private static final int PAGE_NETWORK = 1;
    private static final int PAGE_DEVICES = 2;
    private static final int PAGE_WORKBENCH = 3;
    private static final int PAGE_MORE = 4;

    private EditText name_input;
    private EditText host_input;
    private TextView top_session_summary;
    private TextView top_nearby;
    private TextView session_state;
    private TextView nearby_metric;
    private TextView discovery_metric;
    private TextView echo_metric;
    private TextView selected_target;
    private TextView probe_status;
    private TextView log_view;
    private TextView peer_id_view;
    private TextView session_id_view;
    private TextView empty_devices;
    private TextView device_limit_note;
    private LinearLayout device_list;
    private ObservedTopologyView topology_view;
    private View[] pages;
    private Button[] navigation;
    private final StringBuilder log_buffer = new StringBuilder();
    private String peer_id;
    private String selected_host;
    private int selected_port = 50002;
    private int selected_page = PAGE_OVERVIEW;
    private volatile Session current;

    private static final class Session {
        final String id = UUID.randomUUID().toString();
        volatile boolean running = true;
        volatile boolean discovery_active;
        volatile boolean echo_active;
        volatile DatagramSocket udp;
        volatile ServerSocket listener;
        WifiManager.MulticastLock wifi_lock;
        final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
        final ThreadPoolExecutor workers = new ThreadPoolExecutor(
            4, 4, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(4));
    }

    @Override public void onCreate(Bundle saved_state) {
        String theme = getSharedPreferences(THEME_PREFERENCES, MODE_PRIVATE)
            .getString(THEME_KEY, "observatory");
        setTheme("atlas".equals(theme)
            ? R.style.Theme_LanAtlas_Atlas : R.style.Theme_LanAtlas_Observatory);
        super.onCreate(saved_state);
        setContentView(R.layout.activity_main);
        peer_id = getPreferences(MODE_PRIVATE).getString("peer_id", null);
        if (peer_id == null) {
            peer_id = UUID.randomUUID().toString();
            getPreferences(MODE_PRIVATE).edit().putString("peer_id", peer_id).apply();
        }
        bind_views();
        setup_navigation();
        findViewById(R.id.start_button).setOnClickListener(view -> start_session());
        findViewById(R.id.stop_button).setOnClickListener(view -> stop_session());
        findViewById(R.id.probe_button).setOnClickListener(view -> probe());
        findViewById(R.id.theme_dark).setOnClickListener(view -> choose_theme("observatory"));
        findViewById(R.id.theme_light).setOnClickListener(view -> choose_theme("atlas"));
        topology_view.set_listener(this::select_target);
        peer_id_view.setText(peer_id);
        if (saved_state != null) selected_page = saved_state.getInt("page", PAGE_OVERVIEW);
        show_page(selected_page);
        update_session_ui(null);
        update_roster_ui(null, Collections.emptyList());
    }

    private void bind_views() {
        name_input = findViewById(R.id.name_input);
        host_input = findViewById(R.id.host_input);
        top_session_summary = findViewById(R.id.top_session_summary);
        top_nearby = findViewById(R.id.top_nearby);
        session_state = findViewById(R.id.session_state);
        nearby_metric = findViewById(R.id.nearby_metric);
        discovery_metric = findViewById(R.id.discovery_metric);
        echo_metric = findViewById(R.id.echo_metric);
        selected_target = findViewById(R.id.selected_target);
        probe_status = findViewById(R.id.probe_status);
        log_view = findViewById(R.id.log_view);
        peer_id_view = findViewById(R.id.peer_id_view);
        session_id_view = findViewById(R.id.session_id_view);
        empty_devices = findViewById(R.id.empty_devices);
        device_limit_note = findViewById(R.id.device_limit_note);
        device_list = findViewById(R.id.device_list);
        topology_view = findViewById(R.id.topology_view);
        pages = new View[]{findViewById(R.id.page_overview), findViewById(R.id.page_network),
            findViewById(R.id.page_devices), findViewById(R.id.page_workbench),
            findViewById(R.id.page_more)};
        navigation = new Button[]{findViewById(R.id.nav_overview),
            findViewById(R.id.nav_network), findViewById(R.id.nav_devices),
            findViewById(R.id.nav_workbench), findViewById(R.id.nav_more)};
    }

    private void setup_navigation() {
        for (int index = 0; index < navigation.length; index++) {
            int page = index;
            navigation[index].setOnClickListener(view -> show_page(page));
        }
    }

    private void show_page(int page) {
        if (page < 0 || page >= pages.length) return;
        selected_page = page;
        for (int index = 0; index < pages.length; index++) {
            pages[index].setVisibility(index == page ? View.VISIBLE : View.GONE);
            navigation[index].setSelected(index == page);
        }
    }

    private void choose_theme(String theme) {
        getSharedPreferences(THEME_PREFERENCES, MODE_PRIVATE)
            .edit().putString(THEME_KEY, theme).apply();
        recreate();
    }

    private void report(Session session, String text) {
        Log.i(TAG, text);
        runOnUiThread(() -> {
            if (session != null && current != session) return;
            log_buffer.append(LocalTime.now().format(LOG_TIME)).append("  ")
                .append(text).append('\n');
            if (log_buffer.length() > 12000) {
                log_buffer.delete(0, log_buffer.length() - 8000);
            }
            log_view.setText(log_buffer.toString());
        });
    }

    private void start_session() {
        stop_session();
        String name = name_input.getText().toString().trim();
        if (!DiscoveryRules.valid_name(name)) {
            name_input.setError(getString(R.string.name_error));
            report(null, getString(R.string.name_error));
            return;
        }
        name_input.setError(null);
        Session session = new Session();
        current = session;
        WifiManager manager = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        if (manager != null) {
            session.wifi_lock = manager.createMulticastLock("lan-manager-test");
            session.wifi_lock.setReferenceCounted(false);
            session.wifi_lock.acquire();
        }
        update_session_ui(session);
        report(session, getString(R.string.started_session, short_id(session.id)));
        session.workers.execute(() -> discovery(session, name));
        session.workers.execute(() -> echo_server(session));
    }

    private void stop_session() {
        Session session = current;
        if (session == null) return;
        current = null;
        synchronized (session) {
            session.running = false;
            if (session.udp != null) session.udp.close();
            try {
                if (session.listener != null) session.listener.close();
            } catch (IOException error) {
                Log.d(TAG, "Listener close", error);
            }
        }
        for (Socket socket : session.sockets) {
            try {
                socket.close();
            } catch (IOException error) {
                Log.d(TAG, "Socket close", error);
            }
        }
        session.workers.shutdownNow();
        if (session.wifi_lock != null && session.wifi_lock.isHeld()) {
            session.wifi_lock.release();
        }
        update_session_ui(null);
        update_roster_ui(null, Collections.emptyList());
        report(null, getString(R.string.networking_stopped));
    }

    @Override protected void onSaveInstanceState(Bundle state) {
        state.putInt("page", selected_page);
        super.onSaveInstanceState(state);
    }

    @Override protected void onStop() {
        stop_session();
        super.onStop();
    }

    private void update_session_ui(Session session) {
        runOnUiThread(() -> {
            if (session != null && current != session) return;
            if (session == null && current != null) return;
            boolean running = session != null && current == session && session.running;
            session_state.setText(running ? R.string.running : R.string.stopped);
            top_session_summary.setText(running
                ? getString(R.string.started_session, short_id(session.id))
                : getString(R.string.foreground_only));
            discovery_metric.setText(running && session.discovery_active
                ? R.string.active : R.string.inactive);
            echo_metric.setText(running && session.echo_active
                ? R.string.listening_port : R.string.inactive);
            session_id_view.setText(running ? session.id : getString(R.string.no_active_session));
        });
    }

    private void discovery(Session session, String name) {
        Map<String, PeerSnapshot> peers = new HashMap<>();
        if (!session.running) return;
        try (DatagramSocket socket = new DatagramSocket(null)) {
            synchronized (session) {
                if (!session.running) return;
                session.udp = socket;
            }
            socket.setReuseAddress(true);
            socket.setBroadcast(true);
            socket.bind(new InetSocketAddress("0.0.0.0", 50000));
            socket.setSoTimeout(250);
            session.discovery_active = true;
            update_session_ui(session);
            JSONObject hello = new JSONObject().put("version", 1).put("peer_id", peer_id)
                .put("session_id", session.id).put("name", name).put("tcp_port", 50002)
                .put("capabilities", new JSONArray().put("echo_v1"));
            byte[] packet = ("LMAN\u0001" + hello).getBytes(StandardCharsets.UTF_8);
            if (packet.length > 1200) throw new IOException("HELLO too large");
            long next_announcement = 0;
            long next_ui = 0;
            byte[] receive_buffer = new byte[65535];
            while (session.running) {
                long now = SystemClock.elapsedRealtime();
                if (now >= next_announcement) {
                    socket.send(new DatagramPacket(packet, packet.length,
                        InetAddress.getByName("255.255.255.255"), 50000));
                    next_announcement = now + 2000;
                }
                long cutoff = now - 6000;
                peers.entrySet().removeIf(entry -> entry.getValue().last_seen_ms <= cutoff);
                DatagramPacket incoming = new DatagramPacket(receive_buffer, receive_buffer.length);
                try {
                    socket.receive(incoming);
                } catch (SocketTimeoutException ignored) {
                    // Receive silence must still allow announcement and expiry checks.
                    incoming = null;
                }
                if (incoming != null) {
                    try {
                        PeerSnapshot peer = decode_hello(incoming, session.id);
                        if (peer != null
                                && (peers.containsKey(peer.session_id) || peers.size() < 256)) {
                            peers.put(peer.session_id, peer);
                        }
                    } catch (JSONException | IllegalArgumentException | IOException error) {
                        Log.w(TAG, "Malformed HELLO", error);
                    }
                }
                now = SystemClock.elapsedRealtime();
                if (now >= next_ui) {
                    List<PeerSnapshot> snapshot = new ArrayList<>(peers.values());
                    snapshot.sort(Comparator.comparing(peer -> peer.name));
                    update_roster_ui(session, snapshot);
                    next_ui = now + 2000;
                }
            }
        } catch (Exception error) {
            if (session.running) {
                report(session, getString(R.string.discovery_stopped, error));
            }
        } finally {
            session.discovery_active = false;
            update_session_ui(session);
        }
    }

    private PeerSnapshot decode_hello(DatagramPacket incoming, String local_session)
            throws IOException, JSONException {
        int size = incoming.getLength();
        byte[] data = incoming.getData();
        if (size < 6 || size > 1200 || data[0] != 'L' || data[1] != 'M'
                || data[2] != 'A' || data[3] != 'N' || data[4] != 1) return null;
        JSONObject other = parse(decode_utf8(data, 5, size - 5));
        if (!Integer.valueOf(1).equals(other.get("version"))) return null;
        String session_id = DiscoveryRules.canonical_uuid(other.getString("session_id"));
        String remote_peer_id = DiscoveryRules.canonical_uuid(other.getString("peer_id"));
        if (session_id.equals(local_session)) return null;
        int port = other.getInt("tcp_port");
        if (port < 1 || port > 65535) return null;
        String name = other.getString("name");
        if (!DiscoveryRules.valid_name(name)) return null;
        JSONArray raw_capabilities = other.getJSONArray("capabilities");
        if (raw_capabilities.length() > 16) return null;
        List<String> capabilities = new ArrayList<>();
        for (int index = 0; index < raw_capabilities.length(); index++) {
            Object item = raw_capabilities.get(index);
            if (!(item instanceof String)
                    || !DiscoveryRules.valid_capability((String) item)) return null;
            capabilities.add((String) item);
        }
        return new PeerSnapshot(remote_peer_id, session_id, name,
            incoming.getAddress().getHostAddress(), port, capabilities,
            SystemClock.elapsedRealtime());
    }

    private void update_roster_ui(Session session, List<PeerSnapshot> peers) {
        runOnUiThread(() -> {
            if (session != null && current != session) return;
            if (session == null && current != null) return;
            int count = peers.size();
            top_nearby.setText(getResources().getQuantityString(
                R.plurals.sessions_nearby, count, count));
            nearby_metric.setText(String.valueOf(count));
            empty_devices.setVisibility(peers.isEmpty() ? View.VISIBLE : View.GONE);
            device_list.removeAllViews();
            LayoutInflater inflater = LayoutInflater.from(this);
            int visible_count = Math.min(peers.size(), 64);
            for (int index = 0; index < visible_count; index++) {
                PeerSnapshot peer = peers.get(index);
                View item = inflater.inflate(R.layout.item_device, device_list, false);
                TextView name = item.findViewById(R.id.device_name);
                TextView endpoint = item.findViewById(R.id.device_endpoint);
                TextView state = item.findViewById(R.id.device_state);
                TextView capabilities = item.findViewById(R.id.device_capabilities);
                TextView identity = item.findViewById(R.id.device_identity);
                Button workbench = item.findViewById(R.id.device_workbench);
                double age = Math.max(0, SystemClock.elapsedRealtime() - peer.last_seen_ms) / 1000.0;
                name.setText(peer.name);
                endpoint.setText(getString(R.string.endpoint_format, peer.ip, peer.tcp_port));
                state.setText(getString(R.string.nearby_state, age));
                capabilities.setText(getString(R.string.capabilities_format,
                    String.join(" · ", peer.capabilities)));
                identity.setText(getString(R.string.device_identity_format,
                    short_id(peer.peer_id), short_id(peer.session_id)));
                workbench.setEnabled(peer.has_capability("echo_v1"));
                workbench.setOnClickListener(view -> select_target(peer));
                if (peer.has_capability("echo_v1")) {
                    item.setOnClickListener(view -> select_target(peer));
                }
                item.setContentDescription(name.getText() + ", " + endpoint.getText()
                    + ", " + state.getText());
                device_list.addView(item);
            }
            device_limit_note.setVisibility(peers.size() > visible_count
                ? View.VISIBLE : View.GONE);
            if (peers.size() > visible_count) {
                device_limit_note.setText(getResources().getQuantityString(
                    R.plurals.showing_devices, peers.size(), visible_count, peers.size()));
            }
            topology_view.set_peers(name_input.getText().toString(),
                peers.subList(0, Math.min(peers.size(), 12)));
        });
    }

    private void select_target(PeerSnapshot peer) {
        host_input.setText(peer.ip);
        selected_host = peer.ip;
        selected_port = peer.tcp_port;
        selected_target.setText(getString(R.string.selected_target_format,
            peer.name, peer.ip, peer.tcp_port));
        show_page(PAGE_WORKBENCH);
    }

    private JSONObject message(Session session, String type, JSONObject body) throws JSONException {
        return new JSONObject().put("version", 1).put("type", type)
            .put("message_id", UUID.randomUUID().toString()).put("peer_id", peer_id)
            .put("session_id", session.id).put("body", body);
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
        if (!Integer.valueOf(1).equals(value.get("version"))) {
            throw new IOException("Unsupported version");
        }
        for (String key : new String[]{"peer_id", "session_id", "message_id"}) {
            DiscoveryRules.canonical_uuid(value.getString(key));
        }
        value.getJSONObject("body");
        return value;
    }

    private void echo_server(Session session) {
        if (!session.running) return;
        try (ServerSocket listener = new ServerSocket()) {
            synchronized (session) {
                if (!session.running) return;
                session.listener = listener;
            }
            listener.setReuseAddress(true);
            listener.bind(new InetSocketAddress("0.0.0.0", 50002));
            listener.setSoTimeout(250);
            session.echo_active = true;
            update_session_ui(session);
            report(session, getString(R.string.listening_port));
            while (session.running) {
                Socket socket;
                try {
                    socket = listener.accept();
                } catch (SocketTimeoutException ignored) {
                    continue;
                }
                session.sockets.add(socket);
                try (socket) {
                    JSONObject request;
                    while (session.running && (request = receive(socket)) != null) {
                        if (!"ECHO".equals(request.getString("type"))) {
                            throw new IOException("Expected ECHO");
                        }
                        JSONObject reply = message(
                            session, "ECHO_REPLY", request.getJSONObject("body"));
                        reply.put("reply_to", request.getString("message_id"));
                        Frames.send(socket.getOutputStream(), reply.toString());
                    }
                } catch (Exception error) {
                    if (session.running) {
                        report(session, getString(R.string.echo_connection_ended, error));
                    }
                } finally {
                    session.sockets.remove(socket);
                }
            }
        } catch (Exception error) {
            if (session.running) report(session, getString(R.string.echo_stopped, error));
        } finally {
            session.echo_active = false;
            update_session_ui(session);
        }
    }

    private void probe() {
        Session session = current;
        String host = host_input.getText().toString().trim();
        if (session == null || host.isEmpty()) {
            host_input.setError(getString(R.string.probe_input_error));
            report(null, getString(R.string.probe_input_error));
            return;
        }
        host_input.setError(null);
        int port = host.equals(selected_host) ? selected_port : 50002;
        probe_status.setText(R.string.probe_running);
        try {
            session.workers.execute(() -> {
                try (Socket socket = new Socket()) {
                    session.sockets.add(socket);
                    try {
                        socket.connect(new InetSocketAddress(host, port), 3000);
                        if (!session.running) return;
                        for (String text : new String[]{"hello", "नमस्ते", repeated_x(100000), "end"}) {
                            JSONObject request = message(
                                session, "ECHO", new JSONObject().put("text", text));
                            Frames.send(socket.getOutputStream(), request.toString());
                            JSONObject reply = receive(socket);
                            if (reply == null || !"ECHO_REPLY".equals(reply.getString("type"))
                                    || !request.getString("message_id").equals(reply.getString("reply_to"))
                                    || !text.equals(reply.getJSONObject("body").getString("text"))) {
                                throw new IOException("Echo mismatch");
                            }
                            int length = text.codePointCount(0, text.length());
                            report(session, getResources().getQuantityString(
                                R.plurals.verified_vector, length, length));
                        }
                        runOnUiThread(() -> {
                            if (current == session) probe_status.setText(R.string.probe_success);
                        });
                    } finally {
                        session.sockets.remove(socket);
                    }
                } catch (Exception error) {
                    report(session, getString(R.string.probe_failed, error));
                    runOnUiThread(() -> {
                        if (current == session) {
                            probe_status.setText(getString(R.string.probe_failed, error));
                        }
                    });
                }
            });
        } catch (java.util.concurrent.RejectedExecutionException error) {
            report(session, getString(R.string.probe_queue_full));
            probe_status.setText(R.string.probe_queue_full);
        }
    }

    private static String short_id(String value) {
        return value.length() <= 8 ? value : value.substring(0, 8) + "…";
    }

    private static String repeated_x(int count) {
        char[] value = new char[count];
        java.util.Arrays.fill(value, 'x');
        return new String(value);
    }

    private static String decode_utf8(byte[] data, int offset, int length) throws IOException {
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(data, offset, length)).toString();
        } catch (java.nio.charset.CharacterCodingException error) {
            throw new IOException("Invalid UTF-8", error);
        }
    }

    private JSONObject parse(String json) throws IOException, JSONException {
        try (JsonReader reader = new JsonReader(new StringReader(json))) {
            Object value = read_json(reader, 0);
            if (!(value instanceof JSONObject) || reader.peek() != JsonToken.END_DOCUMENT) {
                throw new IOException("Expected one JSON object");
            }
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
            case STRING:
                return reader.nextString();
            case BOOLEAN:
                return reader.nextBoolean();
            case NULL:
                reader.nextNull();
                return JSONObject.NULL;
            case NUMBER:
                String number = reader.nextString();
                try {
                    return Integer.valueOf(number);
                } catch (NumberFormatException not_integer) {
                    double value = Double.parseDouble(number);
                    if (!Double.isFinite(value)) throw new IOException("Invalid JSON number");
                    return value;
                }
            default:
                throw new IOException("Invalid JSON token");
        }
    }
}
