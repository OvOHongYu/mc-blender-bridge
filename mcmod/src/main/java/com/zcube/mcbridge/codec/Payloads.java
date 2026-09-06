package com.zcube.mcbridge.codec;

import java.util.List;

/** 中立载荷数据结构（不依赖 Minecraft，供快照服务与一致性测试共用）。 */
public final class Payloads {
    private Payloads() {
    }

    /** 调色板条目：方块分类 + 方块状态 id。 */
    public record PalEntry(int cls, String name) {
    }

    /** 单个 16³ Section；indices 长度 4096，idx=(y<<8)|(z<<4)|x，值为调色板下标。 */
    public record SectionData(List<PalEntry> palette, short[] indices) {
    }

    /** 区块载荷；sections 长度 = secCount，null 表示空 Section。 */
    public record ChunkPayload(String dim, int cx, int cz, int yBottom,
                               List<SectionData> sections) {
    }

    /** 网格 quad：4 顶点×xyz（区块局部坐标，外向 CCW），朝向，方块，AO。 */
    public static final class Quad {
        public final short[] verts;   // 12
        public final int dir;         // 0..5 = +X,-X,+Y,-Y,+Z,-Z
        public final int block;       // 区块级调色板下标
        public final int[] ao;        // 4，与顶点对齐（with_ao=false 时全 3）

        public Quad(short[] verts, int dir, int block, int[] ao) {
            this.verts = verts;
            this.dir = dir;
            this.block = block;
            this.ao = ao;
        }
    }
}
