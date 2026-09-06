package com.zcube.mcbridge.codec;

import com.zcube.mcbridge.codec.Payloads.ChunkPayload;
import com.zcube.mcbridge.codec.Payloads.PalEntry;
import com.zcube.mcbridge.codec.Payloads.SectionData;

import java.util.List;

/** MCC1 编码器（未压缩）。字节布局与 docs/协议规范.md 严格一致：
 * magic "MCC1" | ver u8=1 | dim(u16len+utf8) | cx i32 | cz i32 | yBottom i16 |
 * secCount u8 | present u32(LSB-first) | 每个存在 Section:
 * palSize u16 | 每项 class u8 + name(u16len+utf8) | bits u8 | 位压缩索引。
 * 位流：4096 个索引依次排列，每索引 bits 位、LSB-first（等价 numpy packbits little）。 */
public final class Mcc1Writer {
    public static final byte[] MAGIC = {'M', 'C', 'C', '1'};

    private Mcc1Writer() {
    }

    public static byte[] write(ChunkPayload p) {
        BytesLE out = new BytesLE();
        out.bytes(MAGIC);
        out.u8(1);
        out.str(p.dim());
        out.i32(p.cx()).i32(p.cz()).i16(p.yBottom());
        List<SectionData> secs = p.sections();
        out.u8(secs.size());
        long mask = 0;
        for (int i = 0; i < secs.size(); i++) {
            if (secs.get(i) != null) {
                mask |= 1L << i;
            }
        }
        out.u32(mask);
        for (SectionData sec : secs) {
            if (sec == null) {
                continue;
            }
            List<PalEntry> pal = sec.palette();
            out.u16(pal.size());
            for (PalEntry e : pal) {
                out.u8(e.cls()).str(e.name());
            }
            int bits;
            if (pal.size() == 1) {
                bits = 0;
            } else {
                bits = Math.max(4, 32 - Integer.numberOfLeadingZeros(pal.size() - 1));
            }
            out.u8(bits);
            if (bits > 0) {
                out.bytes(packIndices(sec.indices(), bits));
            }
        }
        return out.toByteArray();
    }

    /** 4096 索引 → LSB-first 位流（与 numpy packbits(bitorder='little') 逐字节一致）。 */
    static byte[] packIndices(short[] indices, int bits) {
        int total = indices.length * bits;
        byte[] out = new byte[(total + 7) >>> 3];
        int bitPos = 0;
        for (short index : indices) {
            int v = index & 0xFFFF;
            for (int b = 0; b < bits; b++) {
                if (((v >> b) & 1) != 0) {
                    out[bitPos >> 3] |= (byte) (1 << (bitPos & 7));
                }
                bitPos++;
            }
        }
        return out;
    }
}
