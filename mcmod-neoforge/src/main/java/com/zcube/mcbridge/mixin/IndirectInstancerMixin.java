package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.capture.FlywheelCapture;
import dev.engine_room.flywheel.api.instance.Instance;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 客户端 mixin（Flywheel）：同 {@link BaseInstancerMixin}，但覆盖 **indirect 后端**
 * （`IndirectInstancer` 自带 createInstance，不继承 BaseInstancer）。
 */
@Mixin(value = dev.engine_room.flywheel.backend.engine.indirect.IndirectInstancer.class,
        remap = false)
public abstract class IndirectInstancerMixin {

    @Inject(method = "createInstance()Ldev/engine_room/flywheel/api/instance/Instance;",
            at = @At("RETURN"), remap = false)
    private void mcbridge$noteInstance(CallbackInfoReturnable<Instance> cir) {
        FlywheelCapture.noteInstance(this, cir.getReturnValue());
    }
}
