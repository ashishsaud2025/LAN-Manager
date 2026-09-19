package org.lanmanager.prototype;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Immutable session observation delivered from discovery to the Android UI. */
public final class PeerSnapshot {
    public final String peer_id;
    public final String session_id;
    public final String name;
    public final String ip;
    public final int tcp_port;
    public final List<String> capabilities;
    public final long last_seen_ms;

    public PeerSnapshot(String peer_id, String session_id, String name, String ip,
                        int tcp_port, List<String> capabilities, long last_seen_ms) {
        this.peer_id = peer_id;
        this.session_id = session_id;
        this.name = name;
        this.ip = ip;
        this.tcp_port = tcp_port;
        this.capabilities = Collections.unmodifiableList(new ArrayList<>(capabilities));
        this.last_seen_ms = last_seen_ms;
    }

    public boolean has_capability(String capability) {
        return capabilities.contains(capability);
    }
}
