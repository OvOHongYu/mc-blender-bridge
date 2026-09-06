package com.zcube.mcbridge.codec;

import com.zcube.mcbridge.codec.Payloads.PalEntry;
import com.zcube.mcbridge.codec.Payloads.Quad;

import java.util.List;

/** MCM1 编码器（未压缩）：
 * magic "MCM1" | ver u8=1 | dim | cx i32 | cz i32 | yBottom i16 |
 * palSize u16 | 每项 class u8 + name | flags u8(bit0 AO) | quadCount u32 |
 * 每 quad: 12×i16 顶点 | dir u8 | block u16 | ao u8×4。 */
public final class Mcm1Writer {
    public static final byte[] MAGIC = {'M', 'C', 'M', '1'};

    private Mcm1Writer() {
    }

    public static byte[] write(String dim, int cx, int cz, int yBottom,
                               List<PalEntry> palette, List<Quad> quads,
                               boolean withAo) {
        BytesLE out = new BytesLE();
        out.bytes(MAGIC);
        out.u8(1);
        out.str(dim);
        out.i32(cx).i32(cz).i16(yBottom);
        out.u16(palette.size());
        for (PalEntry e : palette) {
            out.u8(e.cls()).str(e.name());
        }
        out.u8(withAo ? 1 : 0);
        out.u32(quads.size());
        for (Quad q : quads) {
            for (int i = 0; i < 12; i++) {
                out.i16(q.verts[i]);
            }
            out.u8(q.dir).u16(q.block);
            if (withAo) {
                for (int i = 0; i < 4; i++) {
                    out.u8(q.ao[i]);
                }
            }
        }
        return out.toByteArray();
    }
}
