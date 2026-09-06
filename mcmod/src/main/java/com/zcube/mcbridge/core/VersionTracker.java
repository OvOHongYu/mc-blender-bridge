package com.zcube.mcbridge.core;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** 区块版本号：任何 setBlockState 递增（mixin 注入）。key = (dimHash, cx, cz)。 */
public final class VersionTracker {
    private static final Map<Long, Long> VERSIONS = new ConcurrentHashMap<>();

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
    }

    public static long get(String dim, int cx, int cz) {
        return VERSIONS.getOrDefault(key(dim, cx, cz), 1L);
    }
}
