package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.capture.FlywheelCapture;
import dev.engine_room.flywheel.api.instance.Instance;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 客户端 mixin（Flywheel）：记录 `BaseInstancer#createInstance` 产出的实例对象，
 * 挂到其所属的 (方块, Model) 上，用于读取**实例变换**（位置/旋转）。
 *
 * <p>覆盖 instancing 后端（`InstancedInstancer extends BaseInstancer`）。
 */
@Mixin(value = dev.engine_room.flywheel.backend.engine.BaseInstancer.class, remap = false)
public abstract class BaseInstancerMixin {

    @Inject(method = "createInstance()Ldev/engine_room/flywheel/api/instance/Instance;",
            at = @At("RETURN"), remap = false)
    private void mcbridge$noteInstance(CallbackInfoReturnable<Instance> cir) {
        FlywheelCapture.noteInstance(this, cir.getReturnValue());
    }
}
