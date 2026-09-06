package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.core.VersionTracker;
import net.minecraft.block.BlockState;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.World;
import net.minecraft.world.chunk.WorldChunk;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 区块方块变更时递增版本号，供 Blender 侧增量刷新。
 * 按 Yarn 1.20.1 编写；若映射有出入，注入失败不会导致崩溃（defaultRequire=0）。
 */
@Mixin(WorldChunk.class)
public abstract class WorldChunkMixin {
    @Inject(method = "setBlockState", at = @At("HEAD"))
    private void mcbridge$onSetBlockState(BlockPos pos, BlockState state, boolean moved,
                                          CallbackInfoReturnable<BlockState> cir) {
        WorldChunk self = (WorldChunk) (Object) this;
        World world = self.getWorld();
        if (world != null && !world.isClient()) {
            String dim = world.getRegistryKey().getValue().toString();
            VersionTracker.bump(dim, self.getPos().x, self.getPos().z);
        }
    }
}
