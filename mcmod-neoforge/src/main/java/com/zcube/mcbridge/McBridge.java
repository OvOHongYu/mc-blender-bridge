package com.zcube.mcbridge;

import com.zcube.mcbridge.http.ApiServer;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.common.NeoForge;
import net.neoforged.neoforge.event.server.ServerStartedEvent;
import net.neoforged.neoforge.event.server.ServerStoppingEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * MC Bridge 数据服务模组入口（NeoForge）。
 *
 * 服务器启动后在 127.0.0.1:8788 暴露 HTTP API（仅回环）；
 * 停止时关闭。玩家无需在场：模组按坐标强制加载/生成区块。
 *
 * 与 Fabric 版（mcmod/）行为完全一致：仅加载器胶水不同。
 */
@Mod(McBridge.MODID)
public class McBridge {
    public static final String MODID = "mcbridge";
    public static final Logger LOGGER = LoggerFactory.getLogger("mcbridge");
    public static final int PORT = 8788;

    private ApiServer apiServer;

    public McBridge() {
        NeoForge.EVENT_BUS.addListener(this::onServerStarted);
        NeoForge.EVENT_BUS.addListener(this::onServerStopping);
    }

    private void onServerStarted(ServerStartedEvent event) {
        try {
            apiServer = new ApiServer(event.getServer(), PORT);
            apiServer.start();
            LOGGER.info("MC Bridge API 已启动: http://127.0.0.1:{}  (仅本机回环)", PORT);
        } catch (Exception e) {
            LOGGER.error("MC Bridge API 启动失败", e);
        }
    }

    private void onServerStopping(ServerStoppingEvent event) {
        if (apiServer != null) {
            apiServer.stop();
            LOGGER.info("MC Bridge API 已关闭");
        }
    }
}
