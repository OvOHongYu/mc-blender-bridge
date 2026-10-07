package com.zcube.mcbridge.core;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/** 区块版本号：任何 setBlockState 递增（mixin 注入）。key = (dimHash, cx, cz)。
 *  另维护全局世界修订号（任一变更 +1），供 /api/worldrev 事件驱动轮询（R10）。 */
public final class VersionTracker {
    private static final Map<Long, Long> VERSIONS = new ConcurrentHashMap<>();
    private static final AtomicLong WORLD_REV = new AtomicLong(1);

    private VersionTracker() {
    }

    public static long key(String dim, int cx, int cz) {
        long h = 1125899906842597L;
        for (int i = 0; i < dim.length(); i++) {
            h = 31 * h + dim.charAt(i);
        }
        return (h << 32) ^ ((cx & 0xFFFFFFFFL) << 1) ^ (cz & 0xFFFFFFFFL);
    }

    public static void bump(String dim, int cx, int cz) {
        long k = key(dim, cx, cz);
        VERSIONS.merge(k, 1L, Long::sum);
        WORLD_REV.incrementAndGet();
    }

    public static long get(String dim, int cx, int cz) {
        return VERSIONS.getOrDefault(key(dim, cx, cz), 1L);
    }

    /** 全局世界修订号（R10）：只在有方块变更时变化。 */
    public static long worldRev() {
        return WORLD_REV.get();
    }
}
