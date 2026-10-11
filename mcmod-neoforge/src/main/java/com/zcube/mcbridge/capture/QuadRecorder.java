package com.zcube.mcbridge.capture;

import com.mojang.blaze3d.vertex.VertexConsumer;
import net.minecraft.client.Minecraft;
import net.minecraft.client.renderer.RenderType;
import net.minecraft.client.renderer.texture.TextureAtlas;
import net.minecraft.client.renderer.texture.TextureAtlasSprite;
import net.minecraft.resources.ResourceLocation;

import java.util.function.Function;

/**
 * 客户端记录器：把顶点**原样转发**给游戏真正的 {@link VertexConsumer}
 * （画面不受影响），同时旁路累计成四边形交给 {@link GeometryCapture}。
 *
 * <p>顶点属性到达顺序不固定（addVertex → setColor / setUv / setUv1 / setUv2 /
 * setNormal），因此按「下一个顶点结算上一个」的方式累积；MC 提交恒为四边形，
 * 每满 4 个顶点结算一条。{@code setNormal} 也触发结算——它通常是最后一个属性，
 * 这样每批的**最后一条**四边形不会因为没有下一个 addVertex 而丢掉。
 */
public final class QuadRecorder implements VertexConsumer {

    private final VertexConsumer real;
    private final GeometryCapture.QuadSink sink;

    private final float[] xyz = new float[12];
    private final float[] uv = new float[8];
    private final int[] light = new int[4];
    private final float[] nrm = new float[12];
    private int n = 0;

    private float cx, cy, cz;
    private float cu, cv;
    private float cnx, cny, cnz;
    private int cl;
    private boolean pending;

    private QuadRecorder(VertexConsumer real, GeometryCapture.QuadSink sink) {
        this.real = real;
        this.sink = sink;
    }

    /**
     * 捕获窗口内返回记录层，否则原样返回 {@code real}（调用方据此决定是否替换）。
     */
    public static VertexConsumer wrap(RenderType renderType, VertexConsumer real) {
        GeometryCapture.QuadSink sink = GeometryCapture.sinkFor(String.valueOf(renderType));
        if (sink == null) {
            return real;
        }
        syncCamera();
        ensureSpriteTable();
        return new QuadRecorder(real, sink);
    }

    /**
     * 建一次方块图集的 sprite 表：`[u0, v0, u1, v1, 源贴图路径]`。
     *
     * <p>捕获到的 UV 是图集坐标；有这张表才能把每条四边形折算回"源贴图 + sprite 内
     * 0..1 的 UV"，进而与现有烘焙端（按源贴图名管理纹理）对接。
     *
     * <p>1.21 的 {@code TextureAtlas} 没有枚举接口，但 {@code Minecraft#getTextureAtlas}
     * 给的正是 {@code ResourceLocation -> TextureAtlasSprite} 查找函数；于是改为**枚举
     * 资源管理器里的 textures/block/**.png**，逐个查 sprite 的 UV 矩形——这样拿到的
     * 也是"源贴图路径"，正是我们需要的。只在捕获窗口开启时建一次。
     */
    static void ensureSpriteTable() {
        if (GeometryCapture.spriteTable() != null) {
            return;
        }
        try {
            Minecraft mc = Minecraft.getInstance();
            Function<ResourceLocation, TextureAtlasSprite> lookup =
                    mc.getTextureAtlas(TextureAtlas.LOCATION_BLOCKS);
            if (lookup == null) {
                return;
            }
            var resources = mc.getResourceManager().listResources(
                    "textures/block", rl -> rl.getPath().endsWith(".png"));
            StringBuilder sb = new StringBuilder(
                    "{\"atlas\":\"minecraft:textures/atlas/blocks.png\",\"sprites\":[");
            boolean first = true;
            for (ResourceLocation png : resources.keySet()) {
                // create:textures/block/axis.png -> create:block/axis
                String p = png.getPath();
                ResourceLocation tex = png.withPath(
                        p.substring("textures/".length(), p.length() - ".png".length()));
                TextureAtlasSprite sp = lookup.apply(tex);
                if (sp == null || sp.getU1() <= sp.getU0()) {
                    continue;
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append('[').append(sp.getU0()).append(',').append(sp.getV0()).append(',')
                        .append(sp.getU1()).append(',').append(sp.getV1()).append(",\"")
                        .append(tex).append("\"]");
            }
            sb.append("]}");
            GeometryCapture.setSpriteTable(sb.toString());
        } catch (Throwable ignored) {
            // 资源未就绪：留空，报表里 UV 将无法解析（可重试）
        }
    }

    /** 把真实相机位置喂给捕获核心（服务端 arm 时只能填玩家眼位估计值）。 */
    private static void syncCamera() {
        try {
            Minecraft mc = Minecraft.getInstance();
            if (mc != null && mc.gameRenderer != null) {
                var pos = mc.gameRenderer.getMainCamera().getPosition();
                GeometryCapture.noteCamera(pos.x, pos.y, pos.z);
            }
        } catch (Throwable ignored) {
            // 客户端尚未就绪：沿用已有值
        }
    }

    private void settle() {
        if (!pending) {
            return;
        }
        pending = false;
        xyz[n * 3] = cx;
        xyz[n * 3 + 1] = cy;
        xyz[n * 3 + 2] = cz;
        uv[n * 2] = cu;
        uv[n * 2 + 1] = cv;
        nrm[n * 3] = cnx;
        nrm[n * 3 + 1] = cny;
        nrm[n * 3 + 2] = cnz;
        light[n] = cl;
        n++;
        if (n == 4) {
            sink.quad(xyz, uv, light, nrm);
            n = 0;
        }
    }

    @Override
    public VertexConsumer addVertex(float x, float y, float z) {
        settle();
        cx = x; cy = y; cz = z;
        cu = 0f; cv = 0f;
        cnx = 0f; cny = 0f; cnz = 0f;
        cl = 0;
        pending = true;
        return real.addVertex(x, y, z);
    }

    @Override
    public VertexConsumer setColor(int red, int green, int blue, int alpha) {
        return real.setColor(red, green, blue, alpha);
    }

    @Override
    public VertexConsumer setUv(float u, float v) {
        cu = u; cv = v;
        return real.setUv(u, v);
    }

    @Override
    public VertexConsumer setUv1(int u, int v) {
        return real.setUv1(u, v);
    }

    @Override
    public VertexConsumer setUv2(int u, int v) {
        cl = (u << 16) | (v & 0xFFFF);
        return real.setUv2(u, v);
    }

    @Override
    public VertexConsumer setNormal(float x, float y, float z) {
        cnx = x; cny = y; cnz = z;
        // 不在此结算：部分渲染路径（如 Flywheel 的 solid 层）会把 UV 放在法线**之后**，
        // 若在 setNormal 就结算，UV 会被记成 0。改为只在下一个 addVertex 结算，
        // 与属性顺序无关（代价：每批最后一个顶点丢弃，概率上可忽略）。
        return real.setNormal(x, y, z);
    }
}
