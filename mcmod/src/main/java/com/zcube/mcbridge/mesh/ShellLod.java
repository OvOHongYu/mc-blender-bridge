package com.zcube.mcbridge.mesh;

import com.zcube.mcbridge.codec.Payloads.Quad;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.TreeSet;

/**
 * LOD2 高度壳网格（与 Python core/mesher.shell_lod 逐面一致）。
 * 输入为未加边框的中心区块 (16, H, 16)。
 */
public final class ShellLod {
    private static final boolean[] FLIP_POS = {false, true, false};
    private static final int[] A1 = {1, 0, 0};
    private static final int[] A2 = {2, 2, 1};

    private ShellLod() {
    }

    public static List<Quad> shell(byte[][][] cls, short[][][] gid) {
        int nX = cls.length;
        int h = cls[0].length;
        int nZ = cls[0][0].length;
        int[] top = new int[nX * nZ];
        int[] gidTop = new int[nX * nZ];
        for (int x = 0; x < nX; x++) {
            for (int z = 0; z < nZ; z++) {
                int t = -1;
                int g = 0;
                for (int y = h - 1; y >= 0; y--) {
                    if (cls[x][y][z] != 0) {
                        t = y;
                        g = gid[x][y][z] & 0xFFFF;
                        break;
                    }
                }
                top[x * nZ + z] = t;
                gidTop[x * nZ + z] = g;
            }
        }
        List<Quad> quads = new ArrayList<>();
        // 顶面: (top,gid) 相同矩形合并
        TreeSet<Long> keys = new TreeSet<>();
        long[] key = new long[nX * nZ];
        for (int x = 0; x < nX; x++) {
            for (int z = 0; z < nZ; z++) {
                int t = top[x * nZ + z];
                long k = t < 0 ? 0
                        : (((long) (t + 2)) << 17) | (gidTop[x * nZ + z] + 1L);
                key[x * nZ + z] = k;
                if (k != 0) {
                    keys.add(k);
                }
            }
        }
        for (long k : keys) {
            boolean[] bm = new boolean[nX * nZ];
            int live = 0;
            for (int j = 0; j < bm.length; j++) {
                bm[j] = key[j] == k;
                if (bm[j]) {
                    live++;
                }
            }
            while (live > 0) {
                int x0 = -1;
                int z0 = -1;
                scan:
                for (int x = 0; x < nX; x++) {
                    for (int z = 0; z < nZ; z++) {
                        if (bm[x * nZ + z]) {
                            x0 = x;
                            z0 = z;
                            break scan;
                        }
                    }
                }
                int w = 1;
                while (x0 + w < nX && bm[(x0 + w) * nZ + z0]) {
                    w++;
                }
                int hh = 1;
                hLoop:
                while (z0 + hh < nZ) {
                    for (int t2 = 0; t2 < w; t2++) {
                        if (!bm[(x0 + t2) * nZ + (z0 + hh)]) {
                            break hLoop;
                        }
                    }
                    hh++;
                }
                for (int xx = x0; xx < x0 + w; xx++) {
                    for (int zz = z0; zz < z0 + hh; zz++) {
                        bm[xx * nZ + zz] = false;
                    }
                }
                live -= w * hh;
                int blk = (int) ((k & 0x1FFFFL) - 1);
                int t = (int) ((k >> 17) - 2);
                emitFlat(quads, blk, t + 1, x0, z0, w, hh, 1, true);
            }
        }
        // 裙边
        int[][] dirs = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}};
        for (int[] dir : dirs) {
            int dx = dir[0];
            int dz = dir[1];
            int d = dz == 0 ? 0 : 2;
            boolean positive = (dx + dz) > 0;
            for (int x = 0; x < nX; x++) {
                for (int z = 0; z < nZ; z++) {
                    int t = top[x * nZ + z];
                    if (t < 0) {
                        continue;
                    }
                    int nx = x + dx;
                    int nz = z + dz;
                    int hn = (nx >= 0 && nx < nX && nz >= 0 && nz < nZ)
                            ? top[nx * nZ + nz] : -1;
                    if (t <= hn) {
                        continue;
                    }
                    int blk = gidTop[x * nZ + z];
                    int y0 = hn + 1;
                    int y1 = t + 1;
                    int p;
                    int u0;
                    int v0;
                    int w;
                    int hgt;
                    if (d == 0) {
                        p = dx > 0 ? x + 1 : x;
                        u0 = y0;
                        v0 = z;
                        w = y1 - y0;
                        hgt = 1;
                    } else {
                        p = dz > 0 ? z + 1 : z;
                        u0 = x;
                        v0 = y0;
                        w = 1;
                        hgt = y1 - y0;
                    }
                    emitFlat(quads, blk, p, u0, v0, w, hgt, d, positive);
                }
            }
        }
        return quads;
    }

    private static void emitFlat(List<Quad> quads, int blk, int p, int u0, int v0,
                                 int w, int h, int d, boolean positive) {
        int lu = u0;
        int lv = v0;
        int[] cu = {lu, lu + w, lu + w, lu};
        int[] cv = {lv, lv, lv + h, lv + h};
        boolean flip = positive ? FLIP_POS[d] : !FLIP_POS[d];
        int[] order = flip ? new int[]{0, 3, 2, 1} : new int[]{0, 1, 2, 3};
        int a1 = A1[d];
        int a2 = A2[d];
        short[] verts = new short[12];
        for (int vi = 0; vi < 4; vi++) {
            int ci = order[vi];
            int[] vec = new int[3];
            vec[d] = p;
            vec[a1] = cu[ci];
            vec[a2] = cv[ci];
            verts[vi * 3] = (short) vec[0];
            verts[vi * 3 + 1] = (short) vec[1];
            verts[vi * 3 + 2] = (short) vec[2];
        }
        quads.add(new Quad(verts, d * 2 + (positive ? 0 : 1), blk,
                new int[]{3, 3, 3, 3}));
    }
}
