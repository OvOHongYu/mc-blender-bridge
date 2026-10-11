package com.zcube.mcbridge.mixin;

import com.mojang.blaze3d.vertex.VertexConsumer;
import com.zcube.mcbridge.capture.QuadRecorder;
import net.minecraft.client.renderer.MultiBufferSource;
import net.minecraft.client.renderer.RenderType;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 客户端 mixin：在 {@code MultiBufferSource.BufferSource#getBuffer} 的返回值上套
 * 一层记录用的 {@link VertexConsumer}（R17 验证原型）。
 *
 * <p>选这个注入点的理由：它是**所有**世界几何（地形、实体、方块实体，以及
 * Flywheel 这类第三方渲染引擎取的批次缓冲）汇入 GPU 前的共同咽喉，而且是纯原版
 * 类——不需要依赖 Create/Flywheel 的编译期 API。捕获只在 {@code /api/capture}
 * 开启的一小段窗口内生效；平时 {@code QuadRecorder.wrap} 立即返回原对象，
 * 仅多一次纳秒级判断。
 *
 * <p>放在 mixins.json 的 {@code client} 数组里——专用服务端没有客户端类，该 mixin
 * 不会被应用（{@code defaultRequire = 0} 保证应用失败也不崩）。
 */
@Mixin(MultiBufferSource.BufferSource.class)
public abstract class MultiBufferSourceMixin {

    @Inject(method = "getBuffer", at = @At("RETURN"), cancellable = true)
    private void mcbridge$captureGeometry(RenderType renderType,
                                          CallbackInfoReturnable<VertexConsumer> cir) {
        VertexConsumer real = cir.getReturnValue();
        if (real == null) {
            return;
        }
        VertexConsumer wrapped = QuadRecorder.wrap(renderType, real);
        if (wrapped != real) {
            cir.setReturnValue(wrapped);
        }
    }
}
