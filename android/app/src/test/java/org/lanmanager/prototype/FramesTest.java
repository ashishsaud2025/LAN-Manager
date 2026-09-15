package org.lanmanager.prototype;

import org.junit.Test;
import static org.junit.Assert.*;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;

public class FramesTest {
    @Test public void unicode_and_coalesced_frames() throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        Frames.send(output, "{\"x\":\"é\"}");
        Frames.send(output, "{}");
        byte[] bytes = output.toByteArray();
        assertEquals(10, bytes[3]);
        ByteArrayInputStream input = new ByteArrayInputStream(bytes) {
            @Override public synchronized int read(byte[] data, int offset, int length) {
                return super.read(data, offset, Math.min(1, length));
            }
        };
        assertEquals("{\"x\":\"é\"}", Frames.receive(input));
        assertEquals("{}", Frames.receive(input));
        assertNull(Frames.receive(input));
    }

    @Test public void malformed_frames_are_rejected() throws Exception {
        for (byte[] bytes : new byte[][]{{0,0,0,0}, {-1,-1,-1,-1}, {0,0,0,2,123},
                                        {0,0,0,1,(byte)255}}) {
            assertThrows(IOException.class, () -> Frames.receive(new ByteArrayInputStream(bytes)));
        }
    }
}
