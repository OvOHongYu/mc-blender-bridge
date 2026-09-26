package com.zcube.mcbridge.mesh;

import com.zcube.mcbridge.codec.Payloads.Quad;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.TreeSet;

/**
 * 贪心网格器 —— 与 Python 参考（core/mesher.py）逐面一致的移植。
 *
 * 数据约定:
 *  - cls/gid 为加 1 格空气边框的体积: [x][y][z]，尺寸 (18, H+2, 18)；
 *  - 面可见性: a 空->无面; b=OPAQUE->剔; a=OPAQUE->可见; 透明同类同 id->剔;
 *    液体同类->剔; 其余可见;
 *  - 合并约束 = (方向, 方块, AO签名); AO 遮挡集 = {OPAQUE, NONCUBE};
 *  - 顶点外向 CCW; dir 0..5 = +X,-X,+Y,-Y,+Z,-Z;
 *  - 输出为区块局部坐标（x,z ∈ 0..16, y ∈ 0..H，相对 yBottom）。
 *
 * 本类不依赖 Minecraft，可独立于服务器编译与测试。
 */
public final class GreedyMesher {
    /** 升序 (u,v) 角点顺序在各轴给出的法向: d0->+X, d1->-Y, d2->+Z。 */
    private static final boolean[] FLIP_POS = {false, true, false};
    /** 各轴的“其余两轴升序”映射: d=0 -> (y,z); d=1 -> (x,z); d=2 -> (x,y)。 */
    private static final int[] A1 = {1, 0, 0};
    private static final int[] A2 = {2, 2, 1};

    private GreedyMesher() {
    }

    /** 输出 quad 列表。cls/gid 均为 (18, H+2, 18)。 */
    public static List<Quad> mesh(byte[][][] clsIn, short[][][] gid,
                                  boolean withAo, boolean leavesFast) {
        return mesh(clsIn, gid, null, withAo, leavesFast);
    }

    /**
     * 输出 quad 列表。cls/gid 均为 (18, H+2, 18)。
     * cross: 按 gid 的标记（true = 交叉面片植物）；null = 无交叉面片。
     */
    public static List<Quad> mesh(byte[][][] clsIn, short[][][] gid,
                                  boolean[] cross, boolean withAo, boolean leavesFast) {
        return mesh(clsIn, gid, cross, null, withAo, leavesFast);
    }

    /**
     * bio: 与 cls 同形的群系 id（18, H+2, 18），null = 不下发群系（MCM1 v1）。
     *
     * 群系进入**合并键**（高位 24..31）：不这样做的话一个贪心矩形可能横跨群系边界，
     * 而报文每面只带一个群系 id，边界处就会串色。这也保证每个合并面片整体属于同一
     * 群系，因此 id 与几何是对齐的（客户端可直接按面取色）。
     */
    public static List<Quad> mesh(byte[][][] clsIn, short[][][] gid,
                                  boolean[] cross, byte[][][] bio,
                                  boolean withAo, boolean leavesFast) {
        return mesh(clsIn, gid, cross, bio, null, withAo, leavesFast);
    }

    /**
     * bioNeed: 按 gid 的"是否声明染色"。只有声明染色的方块才把群系并入合并键 ——
     * 与客户端 `_palette_tints` 的 need 同规则，否则未染色方块也会被拆开，
     * 模式 A（本地网格）与模式 B（服务端网格）的面数就不一致了。
     * null = 全部按不染色处理（等同不下发群系）。
     */
    public static List<Quad> mesh(byte[][][] clsIn, short[][][] gid,
                                  boolean[] cross, byte[][][] bio,
                                  boolean[] bioNeed,
                                  boolean withAo, boolean leavesFast) {
        byte[][][] cls = leavesFast ? fastLeaves(clsIn, gid, cross) : clsIn;
        List<Quad> quads = new ArrayList<>();
        int[] dims = {cls.length, cls[0].length, cls[0][0].length};
        for (int d = 0; d < 3; d++) {
            int nD = dims[d];
            int nU = dims[A1[d]];
            int nV = dims[A2[d]];
            int nPlanes = nD - 1;
            for (int positiveI = 0; positiveI < 2; positiveI++) {
                boolean positive = positiveI == 0;
                int[][] key = new int[nPlanes][nU * nV];
                byte[][] aoP = withAo ? new byte[nPlanes][nU * nV] : null;
                for (int i = 0; i < nPlanes; i++) {
                    boolean[][] occl = null;
                    if (withAo) {
                        occl = new boolean[nU][nV];
                        for (int u = 0; u < nU; u++) {
                            for (int v = 0; v < nV; v++) {
                                int cb = positive ? cellC(cls, d, i + 1, u, v)
                                                  : cellC(cls, d, i, u, v);
                                occl[u][v] = cb == BlockClass.OPAQUE
                                        || cb == BlockClass.NONCUBE;
                            }
                        }
                    }
                    for (int u = 0; u < nU; u++) {
                        for (int v = 0; v < nV; v++) {
                            int ca = positive ? cellC(cls, d, i, u, v)
                                              : cellC(cls, d, i + 1, u, v);
                            int cb = positive ? cellC(cls, d, i + 1, u, v)
                                              : cellC(cls, d, i, u, v);
                            int ga = positive ? cellG(gid, d, i, u, v)
                                              : cellG(gid, d, i + 1, u, v);
                            int gb = positive ? cellG(gid, d, i + 1, u, v)
                                              : cellG(gid, d, i, u, v);
                            int k = 0;
                            if (ca != BlockClass.AIR && cb != BlockClass.OPAQUE
                                    && !(ca == BlockClass.TRANSPARENT
                                    && cb == BlockClass.TRANSPARENT && ga == gb)
                                    && !(ca == BlockClass.LIQUID
                                    && cb == BlockClass.LIQUID)) {
                                int sig = 0;
                                if (withAo) {
                                    int c0 = aoCorner(occl, u, v, -1, -1);
                                    int c1 = aoCorner(occl, u, v, +1, -1);
                                    int c2 = aoCorner(occl, u, v, +1, +1);
                                    int c3 = aoCorner(occl, u, v, -1, +1);
                                    sig = c0 | (c1 << 2) | (c2 << 4) | (c3 << 6);
                                }
                                k = ((ga + 1) << 8) | sig;
                                if (bio != null && ga >= 0 && ga < bioNeedLen(bioNeed)
                                        && bioNeed[ga]) {
                                    // 归属格子与 ga 相同（+dir 面属 i，-dir 面属 i+1）
                                    int bo = positive ? cellBio(bio, d, i, u, v)
                                                      : cellBio(bio, d, i + 1, u, v);
                                    k |= (bo & 0xFF) << 24;
                                }
                            }
                            key[i][u * nV + v] = k;
                            if (withAo) {
                                aoP[i][u * nV + v] = (byte) (k & 0xFF);
                            }
                        }
                    }
                }
                zeroWindow(key, positive, nPlanes, nU, nV);
            emitPlanes(quads, key, aoP, d, positive, withAo, nU, nV);
            }
        }
        emitCrosses(quads, cls, gid, cross, bio);
        return quads;
    }

    // ------------------------------------------------------------- 内部 ----

    private static void emitPlanes(List<Quad> quads, int[][] key, byte[][] aoP,
                                   int d, boolean positive, boolean withAo,
                                   int nU, int nV) {
        int nPlanes = key.length;
        for (int i = 0; i < nPlanes; i++) {
            TreeSet<Integer> keys = new TreeSet<>();
            for (int k : key[i]) {
                if (k != 0) {
                    keys.add(k);
                }
            }
            for (int k : keys) {
                boolean[] bm = new boolean[nU * nV];
                int live = 0;
                for (int j = 0; j < bm.length; j++) {
                    bm[j] = key[i][j] == k;
                    if (bm[j]) {
                        live++;
                    }
                }
                while (live > 0) {
                    int u0 = -1;
                    int v0 = -1;
                    scan:
                    for (int u = 0; u < nU; u++) {
                        for (int v = 0; v < nV; v++) {
                            if (bm[u * nV + v]) {
                                u0 = u;
                                v0 = v;
                                break scan;
                            }
                        }
                    }
                    int w = 1;
                    while (u0 + w < nU && bm[(u0 + w) * nV + v0]) {
                        w++;
                    }
                    int h = 1;
                    hLoop:
                    while (v0 + h < nV) {
                        for (int t = 0; t < w; t++) {
                            if (!bm[(u0 + t) * nV + (v0 + h)]) {
                                break hLoop;
                            }
                        }
                        h++;
                    }
                    for (int uu = u0; uu < u0 + w; uu++) {
                        for (int vv = v0; vv < v0 + h; vv++) {
                            bm[uu * nV + vv] = false;
                        }
                    }
                    live -= w * h;
                    emitQuad(quads, k, i, u0, v0, w, h, d, positive,
                            withAo ? aoP[i] : null, nV);
                }
            }
        }
    }

    private static void emitQuad(List<Quad> quads, int k, int i, int u0, int v0,
                                 int w, int h, int d, boolean positive,
                                 byte[] aoPlane, int nV) {
        int blk = (int) ((k >> 8) & 0xFFFF) - 1;      // 掩码：高位 24..31 是群系
        int biome = (k >>> 24) & 0xFF;
        int lu = u0 - 1;
        int lv = v0 - 1;
        int[] cu = {lu, lu + w, lu + w, lu};
        int[] cv = {lv, lv, lv + h, lv + h};
        boolean flip = positive ? FLIP_POS[d] : !FLIP_POS[d];
        int[] order = flip ? new int[]{0, 3, 2, 1} : new int[]{0, 1, 2, 3};
        int a1 = A1[d];
        int a2 = A2[d];
        int[] aoC = new int[4];
        for (int j = 0; j < 4; j++) {
            aoC[j] = aoPlane != null ? aoPlane[u0 * nV + v0] >> (2 * j) & 3 : 3;
        }
        short[] verts = new short[12];
        for (int vi = 0; vi < 4; vi++) {
            int ci = order[vi];
            int[] vec = new int[3];
            vec[d] = i;
            vec[a1] = cu[ci];
            vec[a2] = cv[ci];
            verts[vi * 3] = (short) vec[0];
            verts[vi * 3 + 1] = (short) vec[1];
            verts[vi * 3 + 2] = (short) vec[2];
        }
        int[] ao = new int[4];
        for (int vi = 0; vi < 4; vi++) {
            ao[vi] = aoC[order[vi]];
        }
        quads.add(new Quad(verts, d * 2 + (positive ? 0 : 1), blk, ao, biome));
    }

    private static void zeroWindow(int[][] key, boolean positive,
                                   int nPlanes, int nU, int nV) {
        int killPlane = positive ? 0 : nPlanes - 1;
        Arrays.fill(key[killPlane], 0);
        for (int i = 0; i < nPlanes; i++) {
            int[] kp = key[i];
            for (int u = 0; u < nU; u++) {
                if (u == 0 || u == nU - 1) {
                    Arrays.fill(kp, u * nV, (u + 1) * nV, 0);
                }
            }
            for (int v = 0; v < nV; v++) {
                if (v == 0 || v == nV - 1) {
                    for (int u = 1; u < nU - 1; u++) {
                        kp[u * nV + v] = 0;
                    }
                }
            }
        }
    }

    /** 交叉面片植物: 对角两个面片，dir=0(+X)->side 贴图，AO 恒满。
     *  只发射一次——消费端（Blender）默认双面渲染；再补反向绕序会与之共面重叠。 */
    private static void emitCrosses(List<Quad> quads, byte[][][] cls,
                                    short[][][] gid, boolean[] cross,
                                    byte[][][] bio) {
        if (cross == null) {
            return;
        }
        int X = cls.length;
        int Y = cls[0].length;
        int Z = cls[0][0].length;
        for (int x = 1; x < X - 1; x++) {
            for (int y = 1; y < Y - 1; y++) {
                for (int z = 1; z < Z - 1; z++) {
                    if (cls[x][y][z] == BlockClass.AIR) {
                        continue;
                    }
                    int g = gid[x][y][z] & 0xFFFF;
                    if (g >= cross.length || !cross[g]) {
                        continue;
                    }
                    int bx = x - 1, by = y - 1, bz = z - 1;
                    int[] a = {bx, by, bz, bx + 1, by, bz + 1,
                            bx + 1, by + 1, bz + 1, bx, by + 1, bz};
                    int[] b = {bx, by, bz + 1, bx + 1, by, bz,
                            bx + 1, by + 1, bz, bx, by + 1, bz + 1};
                    int bIdx = bio == null ? 0 : (bio[x][y][z] & 0xFF);
                    for (int[] v : new int[][]{a, b}) {
                        quads.add(new Quad(toShorts(v), (short) 0, (short) g,
                                new int[]{3, 3, 3, 3}, bIdx));
                    }
                }
            }
        }
    }

    private static short[] toShorts(int[] v) {
        short[] out = new short[12];
        for (int i = 0; i < 12; i++) {
            out[i] = (short) v[i];
        }
        return out;
    }

    private static int cellC(byte[][][] c, int d, int i, int u, int v) {
        return switch (d) {
            case 0 -> c[i][u][v];      // (x=i, y=u, z=v)
            case 1 -> c[u][i][v];      // (x=u, y=i, z=v)
            default -> c[u][v][i];     // (x=u, y=v, z=i)
        };
    }

    private static int cellG(short[][][] g, int d, int i, int u, int v) {
        return switch (d) {
            case 0 -> g[i][u][v] & 0xFFFF;
            case 1 -> g[u][i][v] & 0xFFFF;
            default -> g[u][v][i] & 0xFFFF;
        };
    }

    private static int cellBio(byte[][][] b, int d, int i, int u, int v) {
        return switch (d) {
            case 0 -> b[i][u][v] & 0xFF;
            case 1 -> b[u][i][v] & 0xFF;
            default -> b[u][v][i] & 0xFF;
        };
    }

    private static int bioNeedLen(boolean[] bioNeed) {
        return bioNeed == null ? 0 : bioNeed.length;
    }

    /** 经典 AO: 双侧遮挡 -> 0，否则 3 − (s1+s2+corner)。 */
    private static int aoCorner(boolean[][] occl, int u, int v, int du, int dv) {
        boolean s1 = occAt(occl, u + du, v);
        boolean s2 = occAt(occl, u, v + dv);
        boolean co = occAt(occl, u + du, v + dv);
        if (s1 && s2) {
            return 0;
        }
        return 3 - ((s1 ? 1 : 0) + (s2 ? 1 : 0) + (co ? 1 : 0));
    }

    private static boolean occAt(boolean[][] occl, int u, int v) {
        int nU = occl.length;
        int nV = occl[0].length;
        if (u < 0 || u >= nU || v < 0 || v >= nV) {
            return false;
        }
        return occl[u][v];
    }

    private static byte[][][] fastLeaves(byte[][][] cls, short[][][] gid,
                                         boolean[] cross) {
        byte[][][] out = new byte[cls.length][cls[0].length][cls[0][0].length];
        for (int x = 0; x < cls.length; x++) {
            for (int y = 0; y < cls[0].length; y++) {
                for (int z = 0; z < cls[0][0].length; z++) {
                    boolean isCross = cross != null
                            && (gid[x][y][z] & 0xFFFF) < cross.length
                            && cross[gid[x][y][z] & 0xFFFF];
                    out[x][y][z] = (cls[x][y][z] == BlockClass.CUTOUT && !isCross)
                            ? BlockClass.OPAQUE : cls[x][y][z];
                }
            }
        }
        return out;
    }
}
