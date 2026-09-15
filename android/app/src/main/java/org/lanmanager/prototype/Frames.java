package org.lanmanager.prototype;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;

/** Raw frame codec shared by the Android socket workers and JVM tests. */
public final class Frames {
    public static final int MAX_SIZE = 1048576;

    private Frames() {}

    public static byte[] read_exact(InputStream stream, int size, boolean allow_eof)
            throws IOException {
        byte[] bytes = new byte[size];
        int offset = 0;
        while (offset < size) {
            int count = stream.read(bytes, offset, size - offset);
            if (count < 0) {
                if (offset == 0 && allow_eof) return null;
                throw new EOFException("EOF inside frame");
            }
            offset += count;
        }
        return bytes;
    }

    public static String receive(InputStream stream) throws IOException {
        byte[] header = read_exact(stream, 4, true);
        if (header == null) return null;
        int size = ByteBuffer.wrap(header).getInt();
        if (size < 1 || size > MAX_SIZE) throw new IOException("invalid frame length");
        try {
            return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(read_exact(stream, size, false))).toString();
        } catch (CharacterCodingException error) {
            throw new IOException("invalid UTF-8", error);
        }
    }

    public static void send(OutputStream stream, String json) throws IOException {
        byte[] payload = json.getBytes(StandardCharsets.UTF_8);
        if (payload.length < 1 || payload.length > MAX_SIZE)
            throw new IOException("invalid frame length");
        stream.write(ByteBuffer.allocate(4).putInt(payload.length).array());
        stream.write(payload);
        stream.flush();
    }
}
