package com.zcube.mcbridge.mesh;

import net.minecraft.block.BlockState;
import net.minecraft.registry.Registries;
import net.minecraft.util.Identifier;

import java.lang.reflect.Method;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * BlockState → 六类分类。
 * 优先查手工覆盖表（精确、跨版本稳定），未命中时用反射探测官方遮挡判定
 * （canOcclude / isOpaque 等，映射名随版本漂移），再回退到保守规则。
 */
public final class BlockClassifier {
    /** 覆盖表：方块 id（不含命名空间时默认 minecraft:）。 */
    private static final Map<String, Integer> OVERRIDES = Map.ofEntries(
            Map.entry("glass", BlockClass.TRANSPARENT),
            Map.entry("glass_pane", BlockClass.NONCUBE),
            Map.entry("tinted_glass", BlockClass.TRANSPARENT),
            Map.entry("ice", BlockClass.TRANSPARENT),
            Map.entry("water", BlockClass.LIQUID),
            Map.entry("lava", BlockClass.LIQUID),
            Map.entry("oak_leaves", BlockClass.CUTOUT),
            Map.entry("spruce_leaves", BlockClass.CUTOUT),
            Map.entry("birch_leaves", BlockClass.CUTOUT),
            Map.entry("jungle_leaves", BlockClass.CUTOUT),
            Map.entry("acacia_leaves", BlockClass.CUTOUT),
            Map.entry("dark_oak_leaves", BlockClass.CUTOUT),
            Map.entry("mangrove_leaves", BlockClass.CUTOUT),
            Map.entry("cherry_leaves", BlockClass.CUTOUT),
            Map.entry("azalea_leaves", BlockClass.CUTOUT),
            Map.entry("oak_stairs", BlockClass.NONCUBE),
            Map.entry("oak_slab", BlockClass.NONCUBE),
            Map.entry("oak_fence", BlockClass.NONCUBE),
            Map.entry("oak_door", BlockClass.NONCUBE),
            Map.entry("oak_trapdoor", BlockClass.NONCUBE),
            Map.entry("cobblestone_stairs", BlockClass.NONCUBE),
            Map.entry("stone_brick_stairs", BlockClass.NONCUBE),
            Map.entry("grass_block", BlockClass.OPAQUE),
            Map.entry("short_grass", BlockClass.CUTOUT),
            Map.entry("poppy", BlockClass.CUTOUT),
            Map.entry("dandelion", BlockClass.CUTOUT),
            Map.entry("torch", BlockClass.NONCUBE),
            Map.entry("wall_torch", BlockClass.NONCUBE),
            Map.entry("ladder", BlockClass.NONCUBE),
            Map.entry("rail", BlockClass.NONCUBE));

    private static final Map<String, Integer> CACHE = new ConcurrentHashMap<>();
    private static Method mCanOcclude;
    private static Method mOpaque;
    private static boolean reflected;

    private BlockClassifier() {
    }

    public static int classify(BlockState state) {
        if (state.isAir()) {
            return BlockClass.AIR;
        }
        Identifier id = Registries.BLOCK.getId(state.getBlock());
        String path = id.getPath();
        String full = id.toString();
        Integer hit = OVERRIDES.get(path);
        if (hit == null) {
            hit = OVERRIDES.get(full);
        }
        if (hit != null) {
            return hit;
        }
        return CACHE.computeIfAbsent(full, k -> fallback(state, path));
    }

    private static int fallback(BlockState state, String path) {
        // 反射探测（一次性解析方法名）
        if (!reflected) {
            reflected = true;
            for (String name : new String[]{"canOcclude", "isOpaqueFullCube", "isOpaque"}) {
                try {
                    mCanOcclude = state.getClass().getMethod(name);
                    break;
                } catch (ReflectiveOperationException ignored) {
                    // 尝试下一个候选名
                }
            }
            for (String name : new String[]{"isOpaque", "isSolidBlock"}) {
                try {
                    mOpaque = state.getClass().getMethod(name);
                    break;
                } catch (ReflectiveOperationException ignored) {
                    // 尝试下一个候选名
                }
            }
        }
        try {
            if (mCanOcclude != null && (Boolean) mCanOcclude.invoke(state)) {
                return BlockClass.OPAQUE;
            }
        } catch (ReflectiveOperationException ignored) {
            // 回退到保守规则
        }
        String s = path;
        if (s.contains("stairs") || s.contains("slab") || s.contains("fence")
                || s.contains("door") || s.contains("trapdoor") || s.contains("wall")
                || s.contains("torch") || s.contains("pressure_plate")
                || s.contains("button") || s.contains("pane") || s.contains("bars")) {
            return BlockClass.NONCUBE;
        }
        if (s.contains("leaves") || s.endsWith("flower") || s.contains("sapling")
                || s.contains("bush") || s.contains("grass")) {
            return BlockClass.CUTOUT;
        }
        if (s.equals("water") || s.equals("lava") || s.contains("water")) {
            return BlockClass.LIQUID;
        }
        if (s.contains("glass")) {
            return BlockClass.TRANSPARENT;
        }
        return BlockClass.OPAQUE;
    }
}
