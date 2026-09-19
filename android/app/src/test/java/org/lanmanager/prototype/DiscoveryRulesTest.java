package org.lanmanager.prototype;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.IOException;

import org.junit.Test;

public final class DiscoveryRulesTest {
    @Test public void names_enforce_bounds_and_control_characters() {
        assertTrue(DiscoveryRules.valid_name("Android Phone"));
        assertTrue(DiscoveryRules.valid_name("नमस्ते"));
        assertFalse(DiscoveryRules.valid_name("   "));
        assertFalse(DiscoveryRules.valid_name("\u00a0"));
        assertFalse(DiscoveryRules.valid_name("bad\nname"));
        assertFalse(DiscoveryRules.valid_name("x".repeat(81)));
    }

    @Test public void capabilities_are_bounded_ascii_tokens() {
        assertTrue(DiscoveryRules.valid_capability("echo_v1"));
        assertTrue(DiscoveryRules.valid_capability("test-capability"));
        assertFalse(DiscoveryRules.valid_capability(""));
        assertFalse(DiscoveryRules.valid_capability("contains space"));
        assertFalse(DiscoveryRules.valid_capability("écho"));
        assertFalse(DiscoveryRules.valid_capability("x".repeat(65)));
    }

    @Test public void uuid_requires_canonical_lowercase_spelling() throws Exception {
        String uuid = "00000000-0000-4000-8000-000000000001";
        assertEquals(uuid, DiscoveryRules.canonical_uuid(uuid));
        assertThrows(IOException.class,
            () -> DiscoveryRules.canonical_uuid("00000000-0000-4000-8000-00000000000A"));
        assertThrows(IOException.class, () -> DiscoveryRules.canonical_uuid("not-a-uuid"));
    }
}
