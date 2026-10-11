package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.capture.FlywheelCapture;
import dev.engine_room.flywheel.api.visualization.VisualizationContext;
import net.minecraft.world.level.block.entity.BlockEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 客户端 mixin（Flywheel）：在方块视觉**构造**前后置「当前方块」上下文，
 * 使构造期间创建的 instancer 能被归属到正确的方块。
 *
 * <p>Flywheel 是模组类、名字已是运行时（mojmap）名，故 {@code remap = false}。
 * 用完整方法描述符定位 {@code createRaw(VisualizationContext, BlockEntity, float)}。
 */
@Mixin(value = dev.engine_room.flywheel.impl.visualization.storage.BlockEntityStorage.class,
        remap = false)
public abstract class BlockEntityStorageMixin {

    @Inject(method = "createRaw(Ldev/engine_room/flywheel/api/visualization/VisualizationContext;"
            + "Lnet/minecraft/world/level/block/entity/BlockEntity;F)"
            + "Ldev/engine_room/flywheel/api/visual/BlockEntityVisual;",
            at = @At("HEAD"), remap = false)
    private void mcbridge$beginBlock(VisualizationContext ctx, BlockEntity be, float partialTick,
                                     CallbackInfoReturnable<?> cir) {
        FlywheelCapture.beginBlock(be);
    }

    @Inject(method = "createRaw(Ldev/engine_room/flywheel/api/visualization/VisualizationContext;"
            + "Lnet/minecraft/world/level/block/entity/BlockEntity;F)"
            + "Ldev/engine_room/flywheel/api/visual/BlockEntityVisual;",
            at = @At("RETURN"), remap = false)
    private void mcbridge$endBlock(VisualizationContext ctx, BlockEntity be, float partialTick,
                                   CallbackInfoReturnable<?> cir) {
        FlywheelCapture.endBlock();
    }
}
