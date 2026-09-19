package org.lanmanager.prototype;

import java.io.IOException;
import java.util.UUID;

/** Shared validation rules for local input and received HELLO metadata. */
public final class DiscoveryRules {
    private DiscoveryRules() {}

    public static boolean valid_name(String value) {
        if (value == null || value.codePointCount(0, value.length()) > 80
                || value.codePoints().allMatch(character ->
                    Character.isWhitespace(character) || Character.isSpaceChar(character))) return false;
        return value.codePoints().noneMatch(character -> character < 32 || character == 127);
    }

    public static boolean valid_capability(String value) {
        if (value == null || value.isEmpty() || value.length() > 64) return false;
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            if (character > 127 || !(Character.isLetterOrDigit(character)
                    || character == '_' || character == '-')) return false;
        }
        return true;
    }

    public static String canonical_uuid(String value) throws IOException {
        try {
            if (!UUID.fromString(value).toString().equals(value)) {
                throw new IOException("Invalid UUID");
            }
            return value;
        } catch (IllegalArgumentException | NullPointerException error) {
            throw new IOException("Invalid UUID", error);
        }
    }
}
