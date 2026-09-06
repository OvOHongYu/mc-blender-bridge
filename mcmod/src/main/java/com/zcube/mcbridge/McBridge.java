package com.zcube.mcbridge;

import com.zcube.mcbridge.http.ApiServer;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.minecraft.server.MinecraftServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * MC Bridge 数据服务模组入口。
 *
 * 服务器启动后在 127.0.0.1:8788 暴露 HTTP API（仅回环）；
 * 停止时关闭。玩家无需在场：模组按坐标强制加载/生成区块。
 */
public class McBridge implements ModInitializer {
    public static final Logger LOGGER = LoggerFactory.getLogger("mcbridge");
    public static final int PORT = 8788;

    private ApiServer apiServer;

    @Override
    public void onInitialize() {
        ServerLifecycleEvents.SERVER_STARTED.register((MinecraftServer server) -> {
            try {
                apiServer = new ApiServer(server, PORT);
                apiServer.start();
                LOGGER.info("MC Bridge API 已启动: http://127.0.0.1:{}  (仅本机回环)", PORT);
            } catch (Exception e) {
                LOGGER.error("MC Bridge API 启动失败", e);
            }
        });
        ServerLifecycleEvents.SERVER_STOPPING.register((MinecraftServer server) -> {
            if (apiServer != null) {
                apiServer.stop();
                LOGGER.info("MC Bridge API 已关闭");
            }
        });
    }
}
