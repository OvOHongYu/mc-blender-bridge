package com.zcube.mcbridge.mesh;

import net.minecraft.block.BlockState;
import net.minecraft.registry.Registries;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.EmptyBlockView;

import java.util.Map;

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

    private static final Map<BlockState, Integer> CACHE =
            java.util.Collections.synchronizedMap(new java.util.IdentityHashMap<>());

    private BlockClassifier() {
    }

    public static int classify(BlockState state) {
        if (state.isAir()) {
            return BlockClass.AIR;
        }
        Identifier id = Registries.BLOCK.getId(state.getBlock());
        Integer hit = OVERRIDES.get(id.getPath());
        if (hit == null) {
            hit = OVERRIDES.get(id.toString());
        }
        if (hit != null) {
            return hit;
        }
        // BlockState 是全局规范单例，按实例恒等缓存（雪层等方块每层状态形状不同，
        // 不能按方块 id 缓存）；sections() 只在 server 线程调用，无并发写竞争。
        Integer cached = CACHE.get(state);
        if (cached == null) {
            cached = fallback(state, id.getPath());
            CACHE.put(state, cached);
        }
        return cached;
    }

    private static int fallback(BlockState state, String path) {
        // 遮挡判定与原版面剔除同口径：isOpaque() 只是静态标志（楼梯/台阶等
        // "模型非完整立方体"的方块它也可能是 true），真正决定"邻居的面该不该
        // 被剔掉"的是 isOpaqueFullCube = 不透明 && 剔除形状是完整立方体。
        // 用它做 OPAQUE 判据后，任意原版/模组非整方块（箱子/雪层/花盆/花瓶/…）
        // 都不会错误遮挡邻居，无需按名字逐个特判。
        try {
            if (state.isOpaqueFullCube(EmptyBlockView.INSTANCE, BlockPos.ORIGIN)) {
                return BlockClass.OPAQUE;
            }
        } catch (Throwable ignore) {
            // 个别方块取形状需要世界上下文 -> 按"不遮挡"处理
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
        // 非整立方体且认不出语义（雪层/箱子/漏斗/蜡烛…以及任意模组装饰方块）：
        // 保守取 NONCUBE（近似为完整方块但不遮挡）。
        return BlockClass.NONCUBE;
    }
}
