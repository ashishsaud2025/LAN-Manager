package org.lanmanager.prototype;

import java.io.IOException;
import java.util.Base64;

/** Shared bounds and byte validation for Android file transfers. */
public final class TransferRules {
    public static final int CHUNK_SIZE = 64 * 1024;
    public static final long MAX_FILE_SIZE = 1024L * 1024L * 1024L;
    public static final int MAX_ENCODED_CHUNK = 4 * ((CHUNK_SIZE + 2) / 3);

    private TransferRules() {}

    public static String safe_file_name(String value) throws IOException {
        if (value == null) throw new IOException("file name is unavailable");
        StringBuilder result = new StringBuilder();
        int[] count = {0};
        value.codePoints().forEach(character -> {
            if (count[0] >= 255) return;
            boolean forbidden = character < 32
                || character == '/' || character == '\\' || character == ':';
            result.appendCodePoint(forbidden ? '_' : character);
            count[0]++;
        });
        String name = result.toString();
        if (!valid_file_name(name)) {
            throw new IOException("file name is invalid");
        }
        return name;
    }

    public static boolean valid_file_name(String value) {
        if (value == null || value.trim().isEmpty() || ".".equals(value) || "..".equals(value)
                || value.codePointCount(0, value.length()) > 255) {
            return false;
        }
        return value.codePoints().noneMatch(character -> character < 32
            || character == '/' || character == '\\' || character == ':');
    }

    public static boolean valid_digest(String value) {
        return value != null && value.matches("[0-9a-f]{64}");
    }

    public static byte[] decode_chunk(String value) throws IOException {
        if (value == null || value.isEmpty() || value.length() > MAX_ENCODED_CHUNK) {
            throw new IOException("encoded chunk exceeds the limit");
        }
        try {
            byte[] decoded = Base64.getDecoder().decode(value);
            if (decoded.length < 1 || decoded.length > CHUNK_SIZE) {
                throw new IOException("decoded chunk exceeds the limit");
            }
            return decoded;
        } catch (IllegalArgumentException error) {
            throw new IOException("chunk is not valid Base64", error);
        }
    }

    public static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) result.append(String.format("%02x", item & 0xff));
        return result.toString();
    }
}
