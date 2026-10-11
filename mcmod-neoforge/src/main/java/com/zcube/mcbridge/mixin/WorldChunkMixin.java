package com.zcube.mcbridge.mixin;

import com.zcube.mcbridge.core.VersionTracker;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.LevelChunk;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * 区块方块变更时递增版本号，供 Blender 侧增量刷新（NeoForge / Mojmap）。
 *
 * 1.21.1 中 LevelChunk 的 setBlockState 仅有 (BlockPos, BlockState, boolean moved)
 * 一个重载（与 Fabric 版 Yarn 签名一致）；NeoForge 21.1 运行时即 mojmap 名，
 * 无需 refmap。若注入失败不会导致崩溃（defaultRequire=0，见 mixins.json）。
 */
@Mixin(LevelChunk.class)
public abstract class WorldChunkMixin {
    @Inject(method = "setBlockState", at = @At("HEAD"))
    private void mcbridge$onSetBlockState(BlockPos pos, BlockState state, boolean moved,
                                          CallbackInfoReturnable<BlockState> cir) {
        LevelChunk self = (LevelChunk) (Object) this;
        Level level = self.getLevel();
        if (level != null && !level.isClientSide) {
            String dim = level.dimension().location().toString();
            VersionTracker.bump(dim, self.getPos().x, self.getPos().z);
        }
    }
}
