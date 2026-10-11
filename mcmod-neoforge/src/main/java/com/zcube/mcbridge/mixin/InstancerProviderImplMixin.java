package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.capture.FlywheelCapture;
import dev.engine_room.flywheel.api.instance.InstanceType;
import dev.engine_room.flywheel.api.instance.Instancer;
import dev.engine_room.flywheel.api.model.Model;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 客户端 mixin（Flywheel）：记录每次 `instancer(InstanceType, Model, bias)` 创建的
 * **几何模型**，并归属到当前正在构造的方块视觉（见 {@link BlockEntityStorageMixin}）。
 *
 * <p>{@code InstancerProvider} 的两参重载是默认方法、会转调这个三参实现，
 * 因此只需挂这一处即可覆盖全部创建。
 */
@Mixin(value = dev.engine_room.flywheel.backend.engine.InstancerProviderImpl.class, remap = false)
public abstract class InstancerProviderImplMixin {

    @Inject(method = "instancer(Ldev/engine_room/flywheel/api/instance/InstanceType;"
            + "Ldev/engine_room/flywheel/api/model/Model;I)"
            + "Ldev/engine_room/flywheel/api/instance/Instancer;",
            at = @At("RETURN"), remap = false)
    private void mcbridge$recordInstancer(InstanceType<?> type, Model model, int bias,
                                          CallbackInfoReturnable<Instancer<?>> cir) {
        FlywheelCapture.recordInstancer(type, model, cir.getReturnValue());
    }
}
