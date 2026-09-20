package org.lanmanager.prototype;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.security.MessageDigest;
import java.util.Base64;

import org.junit.Test;

public final class TransferRulesTest {
    @Test public void file_names_are_safe_for_the_shared_protocol() throws Exception {
        assertEquals("photo.jpg", TransferRules.safe_file_name("photo.jpg"));
        assertEquals("folder_file_name", TransferRules.safe_file_name("folder/file:name"));
        assertThrows(IOException.class, () -> TransferRules.safe_file_name(".."));
        assertEquals(255, TransferRules.safe_file_name("😀".repeat(300))
            .codePointCount(0, TransferRules.safe_file_name("😀".repeat(300)).length()));
        assertTrue(TransferRules.valid_file_name(" spaced name "));
        assertTrue(TransferRules.valid_file_name("delete\u007fkey"));
        assertFalse(TransferRules.valid_file_name("folder/file"));
    }

    @Test public void chunks_enforce_base64_and_decoded_bounds() throws Exception {
        byte[] source = new byte[TransferRules.CHUNK_SIZE];
        String encoded = Base64.getEncoder().encodeToString(source);
        assertEquals(source.length, TransferRules.decode_chunk(encoded).length);
        assertThrows(IOException.class, () -> TransferRules.decode_chunk("!!!!"));
        String oversized = Base64.getEncoder().encodeToString(
            new byte[TransferRules.CHUNK_SIZE + 1]);
        assertThrows(IOException.class, () -> TransferRules.decode_chunk(oversized));
    }

    @Test public void digest_format_matches_sha256() throws Exception {
        String digest = TransferRules.hex(MessageDigest.getInstance("SHA-256").digest(
            "hello".getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        assertTrue(TransferRules.valid_digest(digest));
        assertFalse(TransferRules.valid_digest(digest.toUpperCase()));
        assertFalse(TransferRules.valid_digest("0".repeat(63)));
    }
}
