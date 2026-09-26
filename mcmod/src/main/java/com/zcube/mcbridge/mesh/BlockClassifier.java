package com.zcube.mcbridge.mesh;

import net.minecraft.block.BlockState;
import net.minecraft.registry.Registries;
import net.minecraft.util.Identifier;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * BlockState → 六类分类。
 * 优先查手工覆盖表（精确、跨版本稳定），未命中时直接调用原版遮挡判定，
 * 再按名字细分，最后对"非不透明且无法识别"的方块保守取 NONCUBE。
 *
 * 注意：**不能**用反射按 Yarn 方法名探测 —— 发布包经 loom 重映射后方法名是
 * intermediary（method_*），字符串字面量不参与重映射，反射永远匹配不到，
 * 会整片掉到兜底分支。直接调用则由编译器写出正确的重映射引用。
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
            Map.entry("grass_block", BlockClass.OPAQUE),
            Map.entry("short_grass", BlockClass.CUTOUT),
            Map.entry("poppy", BlockClass.CUTOUT),
            Map.entry("dandelion", BlockClass.CUTOUT),
            Map.entry("ladder", BlockClass.NONCUBE),
            Map.entry("rail", BlockClass.NONCUBE));

    private static final Map<String, Integer> CACHE = new ConcurrentHashMap<>();

    private BlockClassifier() {
    }

    public static int classify(BlockState state) {
        if (state.isAir()) {
            return BlockClass.AIR;
        }
        Identifier id = Registries.BLOCK.getId(state.getBlock());
        String path = id.getPath();
        Integer hit = OVERRIDES.get(path);
        if (hit == null) {
            hit = OVERRIDES.get(id.toString());
        }
        if (hit != null) {
            return hit;
        }
        return CACHE.computeIfAbsent(id.toString(), k -> fallback(state, path));
    }

    private static int fallback(BlockState state, String path) {
        // 原版判定：完整不透明立方体（stone/木板/模组整方块…）→ 遮挡邻居
        if (state.isOpaque()) {
            return BlockClass.OPAQUE;
        }
        String s = path;
        if (s.contains("glass")) {
            return BlockClass.TRANSPARENT;
        }
        if (s.contains("water") || s.contains("lava")) {
            return BlockClass.LIQUID;
        }
        if (s.contains("leaves") || s.contains("sapling") || s.contains("flower")
                || s.contains("bush") || s.contains("grass") || s.contains("vine")
                || s.contains("roots") || s.contains("crop") || s.contains("plant")
                || s.contains("petal") || s.contains("lichen")) {
            return BlockClass.CUTOUT;
        }
        // 非不透明且认不出形状（雪层/箱子/漏斗/花盆/蜡烛…以及任意模组装饰方块）：
        // 必须按"不遮挡"处理，否则邻居朝它的面会被错误剔除，露出空洞。
        // 保守取 NONCUBE（近似为完整方块但不遮挡）。
        return BlockClass.NONCUBE;
    }
}
