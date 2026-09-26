package com.zcube.mcbridge;

import com.zcube.mcbridge.codec.Mcc1Writer;
import com.zcube.mcbridge.codec.Mcm1Writer;
import com.zcube.mcbridge.codec.Payloads.ChunkPayload;
import com.zcube.mcbridge.codec.Payloads.PalEntry;
import com.zcube.mcbridge.codec.Payloads.Quad;
import com.zcube.mcbridge.codec.Payloads.SectionData;
import com.zcube.mcbridge.mesh.BlockClass;
import com.zcube.mcbridge.mesh.GreedyMesher;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 跨语言一致性测试：用与 tools/gen_fixtures.py（Python 参考实现）完全相同的
 * 合成区块，跑 Java 编码器与网格器，输出必须与夹具逐字节一致。
 *
 * 运行: java -cp . com.zcube.mcbridge.ConformanceTest [-Dfixtures=目录]
 * （`./gradlew test` 也会执行本测试）
 */
public final class ConformanceTest {
    /** 调色板顺序 = Python gen_fixtures.to_payload 的插入序（air 固定首位）。 */
    private static final String[] WORLD_NAMES = {
        "minecraft:air", "minecraft:stone", "minecraft:water",
        "minecraft:glass", "minecraft:oak_leaves", "minecraft:oak_stairs",
        "minecraft:oak_log", "minecraft:poppy", "minecraft:short_grass",
    };
    private static final int[] WORLD_CLASSES = {
        BlockClass.AIR, BlockClass.OPAQUE, BlockClass.LIQUID,
        BlockClass.TRANSPARENT, BlockClass.CUTOUT, BlockClass.NONCUBE,
        BlockClass.OPAQUE, BlockClass.CUTOUT, BlockClass.CUTOUT,
    };

    public static void main(String[] args) throws Exception {
        Path fixtures = Path.of(System.getProperty("fixtures", "../tests/fixtures"));
        byte[] mcc1Fixture = Files.readAllBytes(fixtures.resolve("mcc1_sample.bin"));
        byte[] mcc1BioFixture = Files.readAllBytes(fixtures.resolve("mcc1_biome_sample.bin"));
        byte[] mcm1Fixture = Files.readAllBytes(fixtures.resolve("mcm1_sample.bin"));

        // ---- 重建合成区块（与 gen_fixtures.build_world 相同布局）
        short[][][] world = new short[16][16][16];   // [x][y][z] -> gid
        int n = WORLD_NAMES.length;
        int air = 0, stone = 1, water = 2, glass = 3;
        int leaves = 4, stairs = 5, log = 6;
        int poppy = 7, shortGrass = 8;
        for (int x = 0; x < 16; x++) {
            for (int z = 0; z < 16; z++) {
                for (int y = 0; y < 4; y++) {
                    world[x][y][z] = (short) stone;   // 石板
                }
            }
        }
        for (int x = 10; x < 16; x++) {
            for (int z = 10; z < 16; z++) {
                world[x][4][z] = (short) water;       // 水池
            }
        }
        for (int z = 4; z < 10; z++) {
            for (int y = 4; y < 8; y++) {
                world[2][y][z] = (short) glass;       // 玻璃墙
            }
        }
        for (int x = 12; x < 15; x++) {
            for (int z = 8; z < 11; z++) {
                for (int y = 5; y < 8; y++) {
                    world[x][y][z] = (short) leaves;  // 树叶团
                }
            }
        }
        world[5][4][5] = (short) stairs;
        world[6][4][5] = (short) stairs;
        for (int y = 4; y < 8; y++) {
            world[8][y][8] = (short) log;             // 原木柱
        }
        world[3][4][3] = (short) poppy;               // 交叉面片植物
        world[4][4][4] = (short) shortGrass;

        byte[][][] wcls = new byte[16][16][16];
        for (int x = 0; x < 16; x++) {
            for (int y = 0; y < 16; y++) {
                for (int z = 0; z < 16; z++) {
                    wcls[x][y][z] = (byte) WORLD_CLASSES[world[x][y][z]];
                }
            }
        }

        List<PalEntry> pal = new ArrayList<>();
        for (int i = 0; i < WORLD_NAMES.length; i++) {
            pal.add(new PalEntry(WORLD_CLASSES[i], WORLD_NAMES[i]));
        }

        // ---- MCC1
        short[] indices = new short[4096];
        for (int x = 0; x < 16; x++) {
            for (int y = 0; y < 16; y++) {
                for (int z = 0; z < 16; z++) {
                    indices[(y << 8) | (z << 4) | x] = world[x][y][z];
                }
            }
        }
        ChunkPayload payload = new ChunkPayload("overworld", 0, 0, 0,
                List.of(new SectionData(pal, indices)));
        byte[] mcc1 = Mcc1Writer.write(payload);
        check("MCC1 与 Python 夹具逐字节一致", mcc1, mcc1Fixture);

        // MCC1 v2：同一 Section 带 4×4×4 群系（x4>=2 沙漠，其余平原）
        List<String> bioNames = List.of("minecraft:plains", "minecraft:desert");
        byte[] bioIds = new byte[64];
        for (int y4 = 0; y4 < 4; y4++) {
            for (int z4 = 0; z4 < 4; z4++) {
                for (int x4 = 0; x4 < 4; x4++) {
                    bioIds[(y4 << 4) | (z4 << 2) | x4] = (byte) (x4 >= 2 ? 1 : 0);
                }
            }
        }
        ChunkPayload payloadBio = new ChunkPayload("overworld", 0, 0, 0,
                List.of(new SectionData(pal, indices, bioNames, bioIds)));
        check("MCC1 v2（群系）与 Python 夹具逐字节一致",
                Mcc1Writer.write(payloadBio), mcc1BioFixture);

        // ---- 组装 padded 体积 (18,18,18)：邻居为空气
        byte[][][] pc = new byte[18][18][18];
        short[][][] pg = new short[18][18][18];
        for (int x = 0; x < 16; x++) {
            for (int y = 0; y < 16; y++) {
                for (int z = 0; z < 16; z++) {
                    pc[x + 1][y + 1][z + 1] = (byte) WORLD_CLASSES[world[x][y][z]];
                    pg[x + 1][y + 1][z + 1] = world[x][y][z];
                }
            }
        }
        // 交叉面片标记: CUTOUT 且非树叶（与 Python mesher.mesh_payload 规则一致）
        boolean[] cross = new boolean[pal.size()];
        for (int i = 0; i < pal.size(); i++) {
            cross[i] = pal.get(i).cls() == BlockClass.CUTOUT
                    && !pal.get(i).name().contains("leaves");
        }
        List<Quad> quads = GreedyMesher.mesh(pc, pg, cross, true, false);
        byte[] mcm1 = Mcm1Writer.write("overworld", 0, 0, 0, pal, quads, true);
        check("MCM1 与 Python 夹具逐字节一致", mcm1, mcm1Fixture);

        System.out.println("OK: Java 实现与 Python 参考一致 (quads=" + quads.size() + ")");
    }

    private static void check(String label, byte[] actual, byte[] expected) {
        if (!Arrays.equals(actual, expected)) {
            int n = Math.min(actual.length, expected.length);
            int diff = -1;
            for (int i = 0; i < n; i++) {
                if (actual[i] != expected[i]) {
                    diff = i;
                    break;
                }
            }
            throw new AssertionError(label + " 失败: len " + actual.length
                    + " vs " + expected.length + ", 首个差异@"
                    + diff + (diff >= 0 ? " (" + actual[diff] + " vs " + expected[diff] + ")" : ""));
        }
    }

    private ConformanceTest() {
    }
}
