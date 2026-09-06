package com.zcube.mcbridge.codec;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** 小端二进制写出辅助（与 Python 参考实现的 struct '<...' 完全对应）。 */
public final class BytesLE {
    private final ByteArrayOutputStream out = new ByteArrayOutputStream(1 << 16);

    public byte[] toByteArray() {
        return out.toByteArray();
    }

    public BytesLE u8(int v) {
        out.write(v & 0xFF);
        return this;
    }

    public BytesLE i16(int v) {
        out.write(v & 0xFF);
        out.write((v >> 8) & 0xFF);
        return this;
    }

    public BytesLE u16(int v) {
        return i16(v);
    }

    public BytesLE i32(int v) {
        out.write(v & 0xFF);
        out.write((v >> 8) & 0xFF);
        out.write((v >> 16) & 0xFF);
        out.write((v >> 24) & 0xFF);
        return this;
    }

    public BytesLE u32(long v) {
        return i32((int) v);
    }

    public BytesLE i64(long v) {
        for (int i = 0; i < 8; i++) {
            out.write((int) ((v >> (8 * i)) & 0xFF));
        }
        return this;
    }

    public BytesLE bytes(byte[] b) {
        try {
            out.write(b);
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
        return this;
    }

    /** u16 长度前缀 + UTF-8（与 Python _pack_str 一致）。 */
    public BytesLE str(String s) {
        byte[] b = s.getBytes(StandardCharsets.UTF_8);
        u16(b.length);
        return bytes(b);
    }
}
