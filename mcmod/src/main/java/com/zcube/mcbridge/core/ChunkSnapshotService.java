package com.zcube.mcbridge.core;

import com.zcube.mcbridge.codec.Payloads.ChunkPayload;
import com.zcube.mcbridge.codec.Payloads.PalEntry;
import com.zcube.mcbridge.codec.Payloads.SectionData;
import com.zcube.mcbridge.codec.Payloads.Quad;
import com.zcube.mcbridge.mesh.BlockClass;
import com.zcube.mcbridge.mesh.BlockClassifier;
import com.zcube.mcbridge.mesh.GreedyMesher;
import net.minecraft.block.BlockState;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.decoration.ArmorStandEntity;
import net.minecraft.entity.decoration.painting.PaintingEntity;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.Box;
import net.minecraft.util.math.EulerAngle;
import net.minecraft.world.World;
import net.minecraft.world.chunk.Chunk;
import net.minecraft.world.chunk.ChunkStatus;
import net.minecraft.world.chunk.WorldChunk;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Semaphore;

/**
 * 区块快照服务：把服务器世界读取封装为与 Python 模拟服务器完全同构的载荷。
 *
 * 线程模型（关键正确性约束）:
 *   快照读取必须在 server 线程（server.execute 入队执行），避免与刻逻辑并发；
 *   信号量限制并发快照数，超出即拒绝（HTTP 429），防止卡服务端主循环。
 */
public final class ChunkSnapshotService {
    public static final int WORLD_MIN_Y = -64;
    public static final int WORLD_HEIGHT = 384;
    public static final int SEC_COUNT = WORLD_HEIGHT / 16;

    private final MinecraftServer server;
    private final Semaphore inFlight = new Semaphore(2);

    public ChunkSnapshotService(MinecraftServer server) {
        this.server = server;
    }

    public interface SnapshotFn<T> {
        T run();
    }

    /** 在 server 线程执行并等待结果；拿不到并发额度或超时返回 null（调用方回 429）。 */
    public <T> T snapshot(SnapshotFn<T> fn) {
        if (!inFlight.tryAcquire()) {
            return null;
        }
        try {
            java.util.concurrent.CompletableFuture<T> fut = new java.util.concurrent.CompletableFuture<>();
            server.execute(() -> {
                try {
                    fut.complete(fn.run());
                } catch (Throwable e) {
                    fut.completeExceptionally(e);
                }
            });
            try {
                return fut.get(30, java.util.concurrent.TimeUnit.SECONDS);
            } catch (java.util.concurrent.TimeoutException te) {
                fut.cancel(false);
                return null;
            } catch (java.util.concurrent.ExecutionException ee) {
                Throwable c = ee.getCause();
                if (c instanceof RuntimeException re) {
                    throw re;
                }
                throw new RuntimeException(c);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return null;
            }
        } finally {
            inFlight.release();
        }
    }

    public ServerWorld world(String dim) {
        for (ServerWorld w : server.getWorlds()) {
            if (w.getRegistryKey().getValue().toString().equals(dim)) {
                return w;
            }
        }
        return null;
    }

    /** 读取（必要时强制加载/生成）区块并转为 MCC1 的 section 列表（不带群系）。 */
    public List<SectionData> sections(ServerWorld w, int cx, int cz) {
        return sections(w, cx, cz, false);
    }

    /** 读取区块 -> MCC1 的 section 列表。withBiome=true 时每个 Section 附带
     * 4×4×4 群系（R8），供被动作 A（本地网格）按群系染色。 */
    public List<SectionData> sections(ServerWorld w, int cx, int cz, boolean withBiome) {
        Chunk chunk = w.getChunk(cx, cz, ChunkStatus.FULL, true);
        String dim = w.getRegistryKey().getValue().toString();
        List<SectionData> out = new ArrayList<>(SEC_COUNT);
        for (int si = 0; si < SEC_COUNT; si++) {
            net.minecraft.world.chunk.ChunkSection sec = chunk.getSectionArray()[si];
            if (sec == null || sec.isEmpty()) {
                out.add(null);
                continue;
            }
            // 4096 次本地坐标读取（(y<<8)|(z<<4)|x 顺序），构建本 Section 调色板
            Map<String, Integer> pos = new HashMap<>();
            List<PalEntry> pal = new ArrayList<>();
            short[] indices = new short[4096];
            boolean any = false;
            for (int ly = 0; ly < 16; ly++) {
                for (int lz = 0; lz < 16; lz++) {
                    for (int lx = 0; lx < 16; lx++) {
                        BlockState st = sec.getBlockState(lx, ly, lz);
                        String name = stateName(st);
                        int cls = BlockClassifier.classify(st);
                        short gi = (short) (int) pos.getOrDefault(name, -1);
                        if (gi < 0) {
                            gi = (short) pal.size();
                            pos.put(name, (int) gi);
                            pal.add(new PalEntry(cls, name));
                        }
                        if (cls != BlockClass.AIR) {
                            any = true;
                        }
                        indices[(ly << 8) | (lz << 4) | lx] = gi;
                    }
                }
            }
            if (!any) {
                out.add(null);
            } else {
                SectionBiomes sb = withBiome ? sectionBiomes(w, cx, cz, si) : null;
                out.add(sb == null ? new SectionData(pal, indices)
                        : new SectionData(pal, indices, sb.names(), sb.ids()));
            }
        }
        return out;
    }

    /** Section 的 4×4×4 群系：名字表（下标即该 Section 内的群系 id，空串 = 未知）
     * 与 64 个下标（idx=(y<<4)|(z<<2)|x）。 */
    private record SectionBiomes(List<String> names, byte[] ids) {
    }

    private static SectionBiomes sectionBiomes(ServerWorld w, int cx, int cz, int si) {
        List<String> names = new ArrayList<>();
        Map<String, Byte> idOf = new HashMap<>();
        byte[] ids = new byte[64];
        int baseX = cx * 16;
        int baseZ = cz * 16;
        int baseY = WORLD_MIN_Y + si * 16;
        for (int y4 = 0; y4 < 4; y4++) {
            for (int z4 = 0; z4 < 4; z4++) {
                for (int x4 = 0; x4 < 4; x4++) {
                    String nm = "";
                    try {
                        var hb = w.getBiome(new net.minecraft.util.math.BlockPos(
                                baseX + x4 * 4, baseY + y4 * 4, baseZ + z4 * 4));
                        nm = hb.getKey().map(k -> k.getValue().toString()).orElse("");
                    } catch (Exception ignore) {
                        // 越界/未生成 -> 未知群系（客户端退回常量色）
                    }
                    Byte id = idOf.get(nm);
                    if (id == null) {
                        id = (byte) names.size();
                        idOf.put(nm, id);
                        names.add(nm);
                    }
                    ids[(y4 << 4) | (z4 << 2) | x4] = id;
                }
            }
        }
        return new SectionBiomes(names, ids);
    }

    private static String stateName(BlockState st) {
        Identifier id = net.minecraft.registry.Registries.BLOCK.getId(st.getBlock());
        StringBuilder sb = new StringBuilder(id.toString());
        var props = st.getProperties();
        if (!props.isEmpty()) {
            sb.append('[');
            List<String> kv = new ArrayList<>();
            for (var p : props) {
                kv.add(p.getName() + "=" + nameOf(st.get(p)));
            }
            kv.sort(String::compareTo);
            sb.append(String.join(",", kv));
            sb.append(']');
        }
        return sb.toString();
    }

    private static String nameOf(Comparable<?> v) {
        try {
            var m = v.getClass().getMethod("asString");
            Object r = m.invoke(v);
            return r != null ? r.toString() : v.toString();
        } catch (ReflectiveOperationException e) {
            return v.toString();
        }
    }

    /** MCC1 载荷（ymin/ymax 对齐 16 裁剪；空 section 置 null）。 */
    public ChunkPayload payload(String dim, int cx, int cz, int ymin, int ymax) {
        return payload(dim, cx, cz, ymin, ymax, false);
    }

    /** payload 的 withBiome=true 版本：每个 Section 附带 4×4×4 群系（MCC1 v2）。 */
    public ChunkPayload payload(String dim, int cx, int cz, int ymin, int ymax,
                                boolean withBiome) {
        ServerWorld w = world(dim);
        if (w == null) {
            return null;
        }
        int yb = Math.max(WORLD_MIN_Y, ((ymin - WORLD_MIN_Y) / 16) * 16 + WORLD_MIN_Y);
        int yt = Math.min(WORLD_MIN_Y + WORLD_HEIGHT, ymax);
        int secLo = Math.max(0, (yb - WORLD_MIN_Y) / 16);
        int secHi = Math.min(SEC_COUNT, (yt - WORLD_MIN_Y + 15) / 16);
        List<SectionData> secs = sections(w, cx, cz, withBiome);
        List<SectionData> sub = new ArrayList<>();
        for (int i = 0; i < SEC_COUNT; i++) {
            sub.add(i < secLo || i >= secHi ? null : secs.get(i));
        }
        return new ChunkPayload(dim, cx, cz, WORLD_MIN_Y + secLo * 16, sub);
    }

    /**
     * 区块内实体列表（R5 控制模式）：只提取插件需要的字段（画 / 盔甲架）。
     *
     * 按包围盒查实体（EntityLookup 的区块索引），返回 JSON 友好的 map 列表；
     * 其它实体类型暂不提取（见 docs/roadmap.md R5 待办）。
     */
    public List<Map<String, Object>> entities(ServerWorld w, int cx, int cz,
                                              int ymin, int ymax) {
        Box box = new Box(cx * 16.0, ymin, cz * 16.0,
                          cx * 16.0 + 16.0, ymax, cz * 16.0 + 16.0);
        List<Map<String, Object>> out = new ArrayList<>();
        for (PaintingEntity p : w.getEntitiesByType(EntityType.PAINTING, box, e -> true)) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", "minecraft:painting");
            m.put("Pos", List.of(p.getX(), p.getY(), p.getZ()));
            m.put("facing", p.getHorizontalFacing().getHorizontal());
            m.put("variant", p.getVariant().getIdAsString());
            out.add(m);
        }
        for (ArmorStandEntity a : w.getEntitiesByType(EntityType.ARMOR_STAND, box, e -> true)) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", "minecraft:armor_stand");
            m.put("Pos", List.of(a.getX(), a.getY(), a.getZ()));
            m.put("Rotation", List.of((double) a.getYaw(), (double) a.getPitch()));
            m.put("Small", a.isSmall());
            m.put("ShowArms", a.shouldShowArms());
            m.put("NoBasePlate", a.shouldHideBasePlate());
            m.put("Marker", a.isMarker());
            m.put("Invisible", a.isInvisible());
            Map<String, List<Double>> pose = new LinkedHashMap<>();
            pose.put("Head", euler(a.getHeadRotation()));
            pose.put("Body", euler(a.getBodyRotation()));
            pose.put("LeftArm", euler(a.getLeftArmRotation()));
            pose.put("RightArm", euler(a.getRightArmRotation()));
            pose.put("LeftLeg", euler(a.getLeftLegRotation()));
            pose.put("RightLeg", euler(a.getRightLegRotation()));
            m.put("Pose", pose);
            out.add(m);
        }
        return out;
    }

    private static List<Double> euler(EulerAngle a) {
        return List.of((double) a.getPitch(), (double) a.getYaw(), (double) a.getRoll());
    }

    /** 3×3 邻域 → padded 体积 (18, H+2, 18) → 服务端贪心网格（与 Python 模式 A/B
     * 相同算法）。返回 quad 列表与区块级调色板。
     */
    public record Meshed(List<Quad> quads, List<PalEntry> palette, int yBottom,
                         List<String> biomeNames) {
    }

    public Meshed mesh(String dim, int cx, int cz, int ymin, int ymax,
                       int lod, boolean withAo, boolean leavesFast) {
        ServerWorld w = world(dim);
        if (w == null) {
            return null;
        }
        ChunkPayload center = payload(dim, cx, cz, ymin, ymax);
        int secLo = 0;
        while (center.sections().get(secLo) == null && secLo + 1 < SEC_COUNT) {
            secLo++;
        }
        int secHi = SEC_COUNT;
        while (secHi > secLo + 1 && center.sections().get(secHi - 1) == null) {
            secHi--;
        }
        int hSec = secHi - secLo;
        int H = hSec * 16;

        // 中心区块 → 体积数组（[x][y][z] = (16,H,16)）+ 区块级调色板（air 固定 0）
        Map<String, Short> globalPal = new HashMap<>();
        List<PalEntry> pal = new ArrayList<>();
        globalPal.put("minecraft:air", (short) 0);
        pal.add(new PalEntry(BlockClass.AIR, "minecraft:air"));
        short[][][] gidC = new short[16][H][16];
        byte[][][] clsC = new byte[16][H][16];
        fillVolume(center, secLo, globalPal, pal, gidC, clsC);

        // 3×3 padded (18, H+2, 18)
        byte[][][] cls = new byte[18][H + 2][18];
        short[][][] gid = new short[18][H + 2][18];
        for (int x = 0; x < 16; x++) {
            for (int y = 0; y < H; y++) {
                for (int z = 0; z < 16; z++) {
                    cls[x + 1][y + 1][z + 1] = clsC[x][y][z];
                    gid[x + 1][y + 1][z + 1] = gidC[x][y][z];
                }
            }
        }
        fillEdges(dim, cx, cz, secLo, hSec, globalPal, pal, cls, gid);

        if (lod >= 2) {
            // 裁掉 y 边框后进入壳网格
            byte[][][] c16 = new byte[16][H][16];
            short[][][] g16 = new short[16][H][16];
            for (int x = 0; x < 16; x++) {
                for (int y = 0; y < H; y++) {
                    for (int z = 0; z < 16; z++) {
                        c16[x][y][z] = cls[x + 1][y + 1][z + 1];
                        g16[x][y][z] = gid[x + 1][y + 1][z + 1];
                    }
                }
            }
            var quads = com.zcube.mcbridge.mesh.ShellLod.shell(c16, g16);
            return new Meshed(quads, pal, WORLD_MIN_Y + secLo * 16, List.of());
        }
        // 交叉面片: CUTOUT 且非树叶（与 Python mesher.mesh_payload 规则一致）
        boolean[] cross = new boolean[pal.size()];
        for (int i = 0; i < pal.size(); i++) {
            cross[i] = pal.get(i).cls() == BlockClass.CUTOUT
                    && !pal.get(i).name().contains("leaves");
        }
        // 群系（R8）：4×4×4 采样成 padded id 数组，与 cls/gid 同形；下标即"归属格子"。
        // 服务端把群系并入合并键，因此每个合并面片整体属于同一群系，客户端可按面取色。
        List<String> biomeNames = new ArrayList<>();
        byte[][][] bio = new byte[18][H + 2][18];
        fillBiomes(w, cx, cz, secLo, H, biomeNames, bio);
        List<Quad> quads = GreedyMesher.mesh(cls, gid, cross, bio, bioNeed(pal),
                                             withAo, leavesFast);
        return new Meshed(quads, pal, WORLD_MIN_Y + secLo * 16, biomeNames);
    }

    /**
     * 哪些方块要把群系并入合并键（与客户端 `blocks.py` 共享表的染色声明同源）。
     *
     * 服务端没有资产包，只能按这张小表判断；加载资产包后客户端的面掩码可能更宽
     * （例如草方块侧面的 overlay 层也染色），此时模式 A 会比模式 B 多拆一点。
     * 未声明染色的方块跨群系合并没有观感问题（它们本就不取群系色）。
     */
    private static boolean[] bioNeed(List<PalEntry> pal) {
        boolean[] need = new boolean[pal.size()];
        for (int i = 0; i < pal.size(); i++) {
            String n = pal.get(i).name().toLowerCase();
            need[i] = n.equals("minecraft:grass_block") || n.contains("grass")
                    || n.contains("leaves") || n.equals("minecraft:water")
                    || n.contains("fern");
        }
        return need;
    }

    /**
     * 填充 padded 群系 id 数组。id 0 保留给"未知"（列表首项为空串），名字表下标即 id。
     * 以 4³ 为单位查询（与原版群系精度一致），同一 cell 只查一次。
     */
    private void fillBiomes(ServerWorld w, int cx, int cz, int secLo, int H,
                            List<String> names, byte[][][] bio) {
        names.add("");
        Map<String, Byte> idOf = new HashMap<>();
        Map<Long, Byte> cellCache = new HashMap<>();
        int baseX = cx * 16;
        int baseZ = cz * 16;
        int baseY = WORLD_MIN_Y + secLo * 16;
        for (int px = 0; px < 18; px++) {
            int wx = baseX + px - 1;
            for (int pz = 0; pz < 18; pz++) {
                int wz = baseZ + pz - 1;
                for (int py = 0; py < H + 2; py++) {
                    int wy = baseY + py - 1;
                    long ck = (((long) (wx >> 2)) << 40) | (((long) (wz >> 2)) << 20)
                            | ((wy >> 2) & 0xFFFFFL);
                    Byte id = cellCache.get(ck);
                    if (id == null) {
                        String nm = "";
                        try {
                            var hb = w.getBiome(new net.minecraft.util.math.BlockPos(wx, wy, wz));
                            nm = hb.getKey().map(k -> k.getValue().toString()).orElse("");
                        } catch (Exception ignore) {
                            // 越界/未生成 -> 未知群系（客户端退回常量色）
                        }
                        id = idOf.get(nm);
                        if (id == null) {
                            id = (names.size() > 255) ? (byte) 0 : (byte) names.size();
                            if (id != 0) {
                                names.add(nm);
                                idOf.put(nm, id);
                            }
                        }
                        cellCache.put(ck, id);
                    }
                    bio[px][py][pz] = id;
                }
            }
        }
    }

    private void fillVolume(ChunkPayload center, int secLo,
                            Map<String, Short> globalPal, List<PalEntry> pal,
                            short[][][] gid, byte[][][] cls) {
        for (int si = 0; si < center.sections().size(); si++) {
            SectionData sec = center.sections().get(si);
            if (sec == null) {
                continue;
            }
            short[] remap = new short[sec.palette().size()];
            for (int pi = 0; pi < sec.palette().size(); pi++) {
                PalEntry e = sec.palette().get(pi);
                Short g = globalPal.get(e.name());
                if (g == null) {
                    g = (short) pal.size();
                    globalPal.put(e.name(), g);
                    pal.add(new PalEntry(e.cls(), e.name()));
                }
                remap[pi] = g;
            }
            int y0 = (si - secLo) * 16;
            for (int ly = 0; ly < 16; ly++) {
                for (int lz = 0; lz < 16; lz++) {
                    for (int lx = 0; lx < 16; lx++) {
                        int idx = (ly << 8) | (lz << 4) | lx;
                        short g = remap[sec.indices()[idx]];
                        gid[lx][y0 + ly][lz] = g;
                        cls[lx][y0 + ly][lz] = (byte) pal.get(g).cls();
                    }
                }
            }
        }
    }

    /** 邻居区块只取与中心相邻的一列/一行/角，填入 padded 边框。 */
    private void fillEdges(String dim, int cx, int cz, int secLo, int hSec,
                           Map<String, Short> globalPal, List<PalEntry> pal,
                           byte[][][] cls, short[][][] gid) {
        // west/east 列、south/north 行、四角
        for (int[] d : new int[][]{{-1, 0}, {1, 0}, {0, -1}, {0, 1}, {-1, -1}, {-1, 1}, {1, -1}, {1, 1}}) {
            ChunkPayload p = payload(dim, cx + d[0], cz + d[1], WORLD_MIN_Y + secLo * 16,
                    WORLD_MIN_Y + (secLo + hSec) * 16);
            if (p == null) {
                continue;
            }
            short[][][] ng = new short[16][hSec * 16][16];
            byte[][][] nc = new byte[16][hSec * 16][16];
            fillVolume(p, 0, globalPal, pal, ng, nc);
            int bx = d[0] == -1 ? 0 : (d[0] == 1 ? 17 : -1);
            int bz = d[1] == -1 ? 0 : (d[1] == 1 ? 17 : -1);
            if (d[0] != 0 && d[1] != 0) {
                // 角: 单列 (bx, :, bz)
                int sx = d[0] == -1 ? 15 : 0;
                int sz = d[1] == -1 ? 15 : 0;
                for (int y = 0; y < hSec * 16; y++) {
                    cls[bx][y + 1][bz] = nc[sx][y][sz];
                    gid[bx][y + 1][bz] = ng[sx][y][sz];
                }
            } else if (d[0] != 0) {
                int sx = d[0] == -1 ? 15 : 0;
                for (int z = 0; z < 16; z++) {
                    for (int y = 0; y < hSec * 16; y++) {
                        cls[bx][y + 1][z + 1] = nc[sx][y][z];
                        gid[bx][y + 1][z + 1] = ng[sx][y][z];
                    }
                }
            } else {
                int sz = d[1] == -1 ? 15 : 0;
                for (int x = 0; x < 16; x++) {
                    for (int y = 0; y < hSec * 16; y++) {
                        cls[x + 1][y + 1][bz] = nc[x][y][sz];
                        gid[x + 1][y + 1][bz] = ng[x][y][sz];
                    }
                }
            }
        }
    }
}
