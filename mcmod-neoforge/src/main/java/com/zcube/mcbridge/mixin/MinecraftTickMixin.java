package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.capture.FlywheelCapture;
import net.minecraft.client.Minecraft;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * 客户端 mixin：每帧触发一次 Flywheel 几何采集。
 *
 * <p>Flywheel 的实例化几何不走任何 {@code VertexConsumer}，无法像 R17 那样在顶点
 * 提交时旁路记录；改为在帧末（{@code Minecraft#runTick} 返回时）主动遍历登记表做一次
 * 几何快照（见 {@link FlywheelCapture}）。非捕获窗口时该调用立即返回。
 */
@Mixin(Minecraft.class)
public abstract class MinecraftTickMixin {

    @Inject(method = "runTick", at = @At("RETURN"))
    private void mcbridge$flywheelTick(boolean render, CallbackInfo ci) {
        FlywheelCapture.tick();
    }
}
