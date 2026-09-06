package com.zcube.mcbridge.tex;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 方块贴图服务。
 *
 * 优先级:
 *  1. 配置目录 mcbridge/textures/ 下的 <name>__[top|side|bottom].png（用户手工放置，
 *     可从任意资源包提取 assets/minecraft/textures/block/*.png）；
 *  2. 程序化回退：按方块名的稳定哈希生成 16×16 噪声色块（保证可用，不求美观）。
 *
 * 服务端无原版贴图资产（client.jar 才有），这是设计上诚实的取舍；v2 可支持
 * 直接指定 client.jar / 资源包 zip 自动提取。
 */
public final class TextureService {
    private static final List<Path> SEARCH_DIRS = List.of(Path.of("mcbridge", "textures"));
    private final Map<String, byte[]> cache = new ConcurrentHashMap<>();

    public byte[] png(String block, String face) {
        String key = block + "|" + face;
        return cache.computeIfAbsent(key, k -> load(block, face));
    }

    private byte[] load(String block, String face) {
        String name = block.split(":")[block.contains(":") ? 1 : 0];
        for (Path dir : SEARCH_DIRS) {
            Path p = dir.resolve(name + "__" + face + ".png");
            if (Files.isRegularFile(p)) {
                try {
                    return Files.readAllBytes(p);
                } catch (IOException ignored) {
                    // 回退程序化贴图
                }
            }
        }
        return procedural(name, face);
    }

    private static byte[] procedural(String name, String face) {
        long seed = 17;
        for (int i = 0; i < name.length(); i++) {
            seed = seed * 31 + name.charAt(i);
        }
        int baseR = 96 + (int) ((seed >>> 8) % 96);
        int baseG = 96 + (int) ((seed >>> 16) % 96);
        int baseB = 96 + (int) ((seed >>> 24) % 96);
        BufferedImage img = new BufferedImage(16, 16, TYPE);
        var rand = new java.util.Random(seed);
        for (int y = 0; y < 16; y++) {
            for (int x = 0; x < 16; x++) {
                int n = rand.nextInt(24) - 12;
                int r = clamp(baseR + n), g = clamp(baseG + n), b = clamp(baseB + n);
                img.setRGB(x, y, (0xFF << 24) | (r << 16) | (g << 8) | b);
            }
        }
        try {
            var out = new ByteArrayOutputStream();
            ImageIO.write(img, "png", out);
            return out.toByteArray();
        } catch (IOException e) {
            throw new RuntimeException(e);
        }
    }

    private static final int TYPE = BufferedImage.TYPE_INT_ARGB;

    private static int clamp(int v) {
        return Math.max(0, Math.min(255, v));
    }
}
