package com.zcube.mcbridge.http;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.zcube.mcbridge.McBridge;
import com.zcube.mcbridge.core.ChunkSnapshotService;
import com.zcube.mcbridge.core.VersionTracker;
import com.zcube.mcbridge.codec.Mcc1Writer;
import com.zcube.mcbridge.codec.Mcm1Writer;
import com.zcube.mcbridge.codec.Payloads;
import com.zcube.mcbridge.tex.TextureService;
import net.minecraft.server.MinecraftServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.zip.Deflater;

/**
 * 嵌入式 HTTP API（JDK 自带 HttpServer，零第三方依赖），仅绑定 127.0.0.1。
 *
 * 端点与 docs/协议规范.md 一致:
 *   GET /api/ping                        能力协商（JSON）
 *   GET /api/blocks                      方块分类表（JSON）
 *   GET /api/chunk?dim&cx&cz&ymin&ymax   MCC1（zlib）
 *   GET /api/mesh?...&lod&ao&leaves      MCM1（zlib）
 *   GET /api/entities?dim&cx&cz         实体列表（JSON；R5 画/盔甲架）
 *   GET /api/versions?dim&cx0&cz0&cx1&cz1 版本表（zlib）
 *   GET /api/texture?block&face          PNG（不压缩）
 */
public final class ApiServer {
    private final HttpServer http;
    private final MinecraftServer server;
    private final ChunkSnapshotService snapshots;
    private final TextureService textures;

    public ApiServer(MinecraftServer server, int port) throws IOException {
        this.server = server;
        this.snapshots = new ChunkSnapshotService(server);
        this.textures = new TextureService();
        http = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 64);
        http.createContext("/api", this::route);
        http.setExecutor(Executors.newFixedThreadPool(8));
    }

    public void start() {
        http.start();
    }

    public void stop() {
        http.stop(0);
    }

    private void route(HttpExchange ex) throws IOException {
        try {
            String path = ex.getRequestURI().getPath();
            Query q = new Query(ex.getRequestURI().getRawQuery());
            switch (path) {
                case "/api/ping" -> ping(ex);
                case "/api/blocks" -> blocks(ex);
                case "/api/player" -> {
                    if (ex.getRequestMethod().equals("POST")) {
                        playerSet(ex, q);
                    } else {
                        player(ex);
                    }
                }
                case "/api/chunk" -> chunk(ex, q);
                case "/api/mesh" -> mesh(ex, q);
                case "/api/entities" -> entities(ex, q);
                case "/api/versions" -> versions(ex, q);
                case "/api/texture" -> texture(ex, q);
                default -> json(ex, 404, "{\"error\":\"not found\"}");
            }
        } catch (BusyException e) {
            json(ex, 429, "{\"error\":\"snapshot queue full, retry later\"}");
        } catch (Exception e) {
            McBridge.LOGGER.error("API 处理失败: {}", ex.getRequestURI(), e);
            json(ex, 500, "{\"error\":" + jsonString(String.valueOf(e)) + "}");
        } finally {
            ex.close();
        }
    }

    private void ping(HttpExchange ex) throws IOException {
        StringBuilder dims = new StringBuilder();
        for (var w : server.getWorlds()) {
            if (dims.length() > 0) {
                dims.append(',');
            }
            dims.append("{\"id\":\"").append(w.getRegistryKey().getValue())
                    .append("\",\"minY\":").append(ChunkSnapshotService.WORLD_MIN_Y)
                    .append(",\"height\":").append(ChunkSnapshotService.WORLD_HEIGHT).append('}');
        }
        String body = ("{\"mod\":\"mcbridge\",\"modVersion\":\"1.0.0\",\"mcVersion\":\""
                + server.getVersion() + "\",\"encodings\":[2],\"modes\":[\"raw\",\"mesh\"],"
                + "\"entities\":true,"
                + "\"dims\":[" + dims + "],\"maxQuads\":200000}");
        json(ex, 200, body);
    }

    /** 玩家状态（单人/第一玩家）：位置 + 视角，供 Blender 相机同步。 */
    private void player(HttpExchange ex) throws IOException {
        var players = server.getPlayerManager().getPlayerList();
        if (players.isEmpty()) {
            json(ex, 200, "{\"player\":null}");
            return;
        }
        var p0 = players.get(0);
        String dim = p0.getWorld().getRegistryKey().getValue().toString();
        double x = p0.getX(), y = p0.getY(), z = p0.getZ();
        float yaw = p0.getYaw(), pitch = p0.getPitch();
        String body = ("{\"dim\":\"" + dim + "\",\"x\":" + x + ",\"y\":" + y
                + ",\"z\":" + z + ",\"yaw\":" + yaw + ",\"pitch\":" + pitch + "}");
        json(ex, 200, body);
    }

    /** POST /api/player：把玩家传送到指定位置与视角（Blender 相机 -> 玩家）。 */
    private void playerSet(HttpExchange ex, Query q) throws IOException {
        var players = server.getPlayerManager().getPlayerList();
        if (players.isEmpty()) {
            json(ex, 200, "{\"ok\":false,\"error\":\"no player\"}");
            return;
        }
        var p0 = players.get(0);
        double x = q.getDouble("x", p0.getX());
        double y = q.getDouble("y", p0.getY());
        double z = q.getDouble("z", p0.getZ());
        float yaw = (float) q.getDouble("yaw", p0.getYaw());
        float pitch = (float) q.getDouble("pitch", p0.getPitch());
        // 必须在 server 线程执行（sendPacket / 区块加载线程安全），等待完成
        Boolean ok = snapshots.snapshot(() -> {
            p0.setYaw(yaw);
            p0.setPitch(pitch);
            p0.networkHandler.requestTeleport(x, y, z, yaw, pitch, java.util.Set.of());
            return Boolean.TRUE;
        });
        if (ok == null) {
            json(ex, 429, "{\"ok\":false,\"error\":\"server busy\"}");
            return;
        }
        json(ex, 200, "{\"ok\":true}");
    }

    private void blocks(HttpExchange ex) throws IOException {
        StringBuilder sb = new StringBuilder("{\"blocks\":{");
        boolean first = true;
        for (var entry : net.minecraft.registry.Registries.BLOCK.getEntrySet()) {
            var state = entry.getValue().getDefaultState();
            int cls = com.zcube.mcbridge.mesh.BlockClassifier.classify(state);
            String id = net.minecraft.registry.Registries.BLOCK.getId(entry.getValue()).toString();
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append(jsonString(id)).append(":{\"class\":").append(cls)
                    .append(",\"tint\":null,\"color\":[0.6,0.6,0.6]}");
        }
        sb.append("},\"nBlocks\":").append(net.minecraft.registry.Registries.BLOCK.size()).append('}');
        json(ex, 200, sb.toString());
    }

    private void chunk(HttpExchange ex, Query q) throws IOException {
        String dim = q.get("dim", "minecraft:overworld");
        int cx = q.getInt("cx"), cz = q.getInt("cz");
        int ymin = q.getInt("ymin", ChunkSnapshotService.WORLD_MIN_Y);
        int ymax = q.getInt("ymax", ChunkSnapshotService.WORLD_MIN_Y + ChunkSnapshotService.WORLD_HEIGHT);
        var payload = snapshots.snapshot(() -> snapshots.payload(dim, cx, cz, ymin, ymax));
        if (payload == null) {
            throw new BusyException();
        }
        byte[] body = zlib(Mcc1Writer.write(payload));
        binary(ex, 200, body, "application/octet-stream", 2);
    }

    private void mesh(HttpExchange ex, Query q) throws IOException {
        String dim = q.get("dim", "minecraft:overworld");
        int cx = q.getInt("cx"), cz = q.getInt("cz");
        int ymin = q.getInt("ymin", ChunkSnapshotService.WORLD_MIN_Y);
        int ymax = q.getInt("ymax", ChunkSnapshotService.WORLD_MIN_Y + ChunkSnapshotService.WORLD_HEIGHT);
        int lod = q.getInt("lod", 0);
        boolean ao = !"0".equals(q.get("ao", lod == 0 ? "1" : "0"));
        boolean fast = "fast".equals(q.get("leaves", "fancy"));
        var meshed = snapshots.snapshot(() ->
                snapshots.mesh(dim, cx, cz, ymin, ymax, lod, ao, fast));
        if (meshed == null) {
            throw new BusyException();
        }
        List<Payloads.PalEntry> pal = meshed.palette();
        List<Payloads.Quad> quads = meshed.quads();
        byte[] body = zlib(Mcm1Writer.write(dim, q.getInt("cx"), q.getInt("cz"),
                meshed.yBottom(), pal, quads, lod < 2));
        binary(ex, 200, body, "application/octet-stream", 2);
    }

    private void entities(HttpExchange ex, Query q) throws IOException {
        String dim = q.get("dim", "minecraft:overworld");
        int cx = q.getInt("cx"), cz = q.getInt("cz");
        int ymin = q.getInt("ymin", ChunkSnapshotService.WORLD_MIN_Y);
        int ymax = q.getInt("ymax", ChunkSnapshotService.WORLD_MIN_Y + ChunkSnapshotService.WORLD_HEIGHT);
        var list = snapshots.snapshot(() -> {
            var w = snapshots.world(dim);
            return w == null ? null : snapshots.entities(w, cx, cz, ymin, ymax);
        });
        if (list == null) {
            throw new BusyException();
        }
        StringBuilder sb = new StringBuilder("{\"entities\":[");
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(jsonVal(list.get(i)));
        }
        sb.append("]}");
        json(ex, 200, sb.toString());
    }

    private void versions(HttpExchange ex, Query q) throws IOException {
        String dim = q.get("dim", "minecraft:overworld");
        int cx0 = q.getInt("cx0"), cz0 = q.getInt("cz0");
        int cx1 = q.getInt("cx1"), cz1 = q.getInt("cz1");
        var out = new java.io.ByteArrayOutputStream();
        u32le(out, (cx1 - cx0 + 1) * (cz1 - cz0 + 1));
        for (int cx = cx0; cx <= cx1; cx++) {
            for (int cz = cz0; cz <= cz1; cz++) {
                final int fcx = cx, fcz = cz;
                snapshots.snapshot(() -> {
                    snapshots.world(dim).getChunk(fcx, fcz, net.minecraft.world.chunk.ChunkStatus.FULL, true);
                    return null;
                });
                i32le(out, cx);
                i32le(out, cz);
                i64le(out, VersionTracker.get(dim, cx, cz));
            }
        }
        binary(ex, 200, zlib(out.toByteArray()), "application/octet-stream", 2);
    }

    private void texture(HttpExchange ex, Query q) throws IOException {
        String block = q.get("block", "minecraft:stone");
        String face = q.get("face", "side");
        byte[] png = textures.png(block, face);
        binary(ex, 200, png, "image/png", 0);
    }

    // ------------------------------------------------------------ 工具 ----

    private static void json(HttpExchange ex, int code, String body) throws IOException {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(b);
        }
    }

    private static void binary(HttpExchange ex, int code, byte[] body,
                               String ctype, int enc) throws IOException {
        ex.getResponseHeaders().set("Content-Type", ctype);
        ex.getResponseHeaders().set("X-MCB-Encoding", enc > 0 ? String.valueOf(enc) : "none");
        ex.sendResponseHeaders(code, body.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(body);
        }
    }

    private static byte[] zlib(byte[] data) {
        Deflater def = new Deflater(6);
        def.setInput(data);
        def.finish();
        var out = new java.io.ByteArrayOutputStream(data.length / 2);
        byte[] buf = new byte[1 << 15];
        while (!def.finished()) {
            out.write(buf, 0, def.deflate(buf));
        }
        def.end();
        return out.toByteArray();
    }

    private static void u32le(java.io.ByteArrayOutputStream out, int v) {
        out.write(v & 0xFF);
        out.write((v >> 8) & 0xFF);
        out.write((v >> 16) & 0xFF);
        out.write((v >> 24) & 0xFF);
    }

    private static void i32le(java.io.ByteArrayOutputStream out, int v) {
        u32le(out, v);
    }

    private static void i64le(java.io.ByteArrayOutputStream out, long v) {
        for (int i = 0; i < 8; i++) {
            out.write((int) ((v >> (8 * i)) & 0xFF));
        }
    }

    private static String jsonString(String s) {
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    /** 递归 JSON 序列化（实体端点用；null/列表/map/数字/布尔）。 */
    private static String jsonVal(Object v) {
        if (v == null) {
            return "null";
        }
        if (v instanceof String s) {
            return jsonString(s);
        }
        if (v instanceof Boolean b) {
            return b ? "true" : "false";
        }
        if (v instanceof Number n) {
            return n.toString();
        }
        if (v instanceof List<?> l) {
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < l.size(); i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(jsonVal(l.get(i)));
            }
            return sb.append(']').toString();
        }
        if (v instanceof Map<?, ?> mm) {
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (var e : mm.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                sb.append(jsonString(String.valueOf(e.getKey()))).append(':')
                  .append(jsonVal(e.getValue()));
            }
            return sb.append('}').toString();
        }
        return "null";
    }

    private static final class BusyException extends RuntimeException {
    }
}
