package com.zcube.mcbridge.capture;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 运行时几何捕获的状态与报表核心（R17 验证原型）。
 *
 * <p><b>本类刻意不引用任何客户端类</b>——它被服务端侧（HTTP 端点）与客户端侧
 * （{@code QuadRecorder} + mixin）共同使用。客户端专属的顶点接线放在
 * {@code com.zcube.mcbridge.capture.QuadRecorder} 里。这样专用服务端加载本类不会
 * 因缺 {@code net.minecraft.client.*} 而失败。
 *
 * <p>用途：验证「能否在客户端把方块真实渲染的几何抓下来」——尤其是 Create 这类
 * 由 Flywheel 实例化绘制、静态资源里拿不到的机器部件。
 *
 * <p>本原型只回答三个问题，不产出正式模型：
 * <ol>
 *   <li>Flywheel 的几何是否经过 {@code MultiBufferSource.BufferSource}（拦不拦得到）；</li>
 *   <li>它走哪些 RenderType、各自产生多少四边形；</li>
 *   <li>提交的坐标是**世界坐标**还是**相机相对坐标**（决定后续如何切到方块局部空间）。</li>
 * </ol>
 */
public final class GeometryCapture {

    /** 每个 RenderType 最多留存多少条「命中目标盒」的四边形（防爆内存）。 */
    private static final int MAX_QUADS_PER_TYPE = 60000;
    /** 记录一个四边形的最小数据载体。 */
    public interface QuadSink {
        /**
         * @param xyz12 4 顶点位置（相机相对坐标，MC 单位）
         * @param uv8   4 顶点 UV（**图集坐标** 0..1）
         * @param light4 4 顶点光照（打包的 lightmap 值）
         * @param nrm12 4 顶点法线（用于定面朝向；可能全 0）
         */
        void quad(float[] xyz12, float[] uv8, int[] light4, float[] nrm12);
    }

    private static volatile boolean armed = false;
    private static volatile long deadlineNanos = 0L;
    private static volatile double tx, ty, tz;
    private static volatile double radius = 2.5;
    private static volatile double camX, camY, camZ;
    private static volatile long armedAtMillis = 0L;
    private static final AtomicBoolean writing = new AtomicBoolean(false);
    private static volatile String lastText = null;
    private static volatile String lastJson = null;
    /** 方块图集的 sprite 表（UV 矩形 -> 源贴图路径）；由客户端在捕获窗口开始时建一次。 */
    private static volatile String spriteTable = null;

    private static final Map<String, TypeStat> STATS = new ConcurrentHashMap<>();

    private GeometryCapture() {
    }

    /** 开始一次捕获。 */
    public static synchronized void arm(double x, double y, double z, double r, int millis,
                                        double cameraX, double cameraY, double cameraZ) {
        tx = x; ty = y; tz = z;
        radius = r > 0 ? r : 2.5;
        camX = cameraX; camY = cameraY; camZ = cameraZ;
        armedAtMillis = System.currentTimeMillis();
        STATS.clear();
        lastText = null;
        lastJson = null;
        spriteTable = null;          // 每轮重建，保证与当前资源包一致
        writing.set(false);
        deadlineNanos = System.nanoTime() + Math.max(100, millis) * 1_000_000L;
        armed = true;
    }

    /** 强制结束当前捕获（未开始则无操作）。 */
    public static void disarm() {
        if (armed) {
            finish();
        }
    }

    /** 是否处于捕获窗口；超时则自动结算。渲染线程高频调用。 */
    public static boolean armedNow() {
        if (!armed) {
            return false;
        }
        if (System.nanoTime() > deadlineNanos) {
            finish();
            return false;
        }
        return true;
    }

    /**
     * 取某个 RenderType 的记录入口；不在捕获窗口时返回 {@code null}（调用方原样转发顶点）。
     */
    public static QuadSink sinkFor(String typeName) {
        if (!armedNow()) {
            return null;
        }
        // 按方块归属的 sink：方块中心在捕获范围内才建立（收录其全部几何）
        if (typeName.startsWith("flywheel@")) {
            try {
                String[] p = typeName.substring("flywheel@".length()).split(",");
                double bx = Double.parseDouble(p[0]) + 0.5;
                double by = Double.parseDouble(p[1]) + 0.5;
                double bz = Double.parseDouble(p[2]) + 0.5;
                double rr = radius + 1.0;
                if (Math.abs(bx - tx) > rr || Math.abs(by - ty) > rr
                        || Math.abs(bz - tz) > rr) {
                    return null;
                }
            } catch (Throwable ignored) {
            }
        }
        return STATS.computeIfAbsent(typeName, TypeStat::new);
    }

    /**
     * 客户端在捕获窗口内刷新真实相机位置（服务端 arm 时只能填玩家眼位的估计值）。
     *
     * <p>用于区分「世界坐标」与「相机相对坐标」两种提交约定——两者相差一个相机位置，
     * 必须用真实值才能判准。
     */
    public static void noteCamera(double x, double y, double z) {
        camX = x;
        camY = y;
        camZ = z;
    }

    /** 捕获结束：结算报表（存内存 + 落盘），异步写文件。 */
    private static synchronized void finish() {
        armed = false;
        long elapsed = System.currentTimeMillis() - armedAtMillis;
        Map<String, TypeStat> snapshot = new LinkedHashMap<>();
        STATS.entrySet().stream().sorted(Map.Entry.comparingByKey())
                .forEach(e -> snapshot.put(e.getKey(), e.getValue()));
        lastText = buildText(snapshot, elapsed);
        lastJson = buildJson(snapshot, elapsed);
        System.out.println(lastText);
        if (!writing.compareAndSet(false, true)) {
            return;
        }
        final String text = lastText;
        final String json = lastJson;
        Thread t = new Thread(() -> {
            try {
                Path dir = Paths.get(System.getProperty("user.dir", "."), "mcbridge-capture");
                Files.createDirectories(dir);
                Files.write(dir.resolve("capture.txt"), text.getBytes(StandardCharsets.UTF_8));
                Files.write(dir.resolve("capture.json"), json.getBytes(StandardCharsets.UTF_8));
                System.out.println("[mcbridge] 捕获报表已写入: " + dir.resolve("capture.txt"));
            } catch (IOException e) {
                System.out.println("[mcbridge] 捕获报表写入失败: " + e);
            }
        }, "mcbridge-capture-writer");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------ 报表 ----

    /** 最近一次捕获的文本报表；尚未捕获返回 null。 */
    public static String lastText() {
        return lastText;
    }

    /** 最近一次捕获的 JSON 报表；尚未捕获返回 null。 */
    public static String lastJson() {
        return lastJson;
    }

    /**
     * 记录方块图集的 sprite 表（客户端一次性建好）。
     *
     * <p>捕获到的 UV 是**图集坐标**，必须靠这张表反查源贴图路径 + 折算到 sprite 内部
     * 的 0..1，才能接回现有烘焙端（烘焙端按源贴图名管理纹理）。
     */
    public static void setSpriteTable(String json) {
        spriteTable = json;
    }

    /** sprite 表 JSON；客户端尚未建立时返回 null。 */
    public static String spriteTable() {
        return spriteTable;
    }

    /** 一行状态，供 /api/capture 立即回执。 */
    public static String statusLine() {
        return String.format(Locale.ROOT,
                "armed=%s target=(%.1f, %.1f, %.1f) r=%.1f", armed, tx, ty, tz, radius);
    }

    // ------------------------------------------------- 客户端采集只读访问 ----

    /** 是否处于捕获窗口（会触发超时结算）。供客户端 Flywheel 采集侧轮询。 */
    public static boolean capturing() {
        return armedNow();
    }

    /** 相机位置（世界坐标）；客户端采集用它把方块局部坐标换算成相机相对坐标。 */
    public static double[] cameraPos() {
        return new double[]{camX, camY, camZ};
    }

    /** 捕获目标与半径：{tx, ty, tz, radius}。 */
    public static double[] captureBox() {
        return new double[]{tx, ty, tz, radius};
    }

    private static String buildText(Map<String, TypeStat> stats, long elapsedMs) {
        StringBuilder sb = new StringBuilder();
        sb.append("MC Bridge 运行时几何捕获报表\n");
        sb.append(String.format(Locale.ROOT, "目标=(%.1f, %.1f, %.1f) 半径=%.1f 相机=(%.1f, %.1f, %.1f) 窗口=%dms%n",
                tx, ty, tz, radius, camX, camY, camZ, elapsedMs));
        sb.append('\n');
        sb.append(String.format("%-44s %10s %10s %10s %s%n",
                "renderType", "四边形", "命中盒", "顶点", "x 范围"));
        int totalQuads = 0;
        int totalHits = 0;
        int worldHits = 0;
        for (TypeStat st : stats.values()) {
            totalQuads += st.quads;
            totalHits += st.boxQuads.size();
            worldHits += st.hitWorld;
            sb.append(String.format(Locale.ROOT, "%-44s %10d %10d %10d %s%n",
                    st.name, st.quads, st.boxQuads.size(), st.vertices,
                    st.hasBounds() ? String.format(Locale.ROOT, "[%.1f .. %.1f]", st.minX, st.maxX) : "-"));
        }
        sb.append(String.format(Locale.ROOT, "%n合计: %d 四边形；命中目标盒 %d 条（其中按世界坐标命中 %d 条）%n",
                totalQuads, totalHits, worldHits));
        sb.append("\n判读提示:\n")
                .append("  · 某行 renderType 属于实体/方块实体层且「命中盒」>0 → 该几何可捕获；\n")
                .append("  · 若命中为 0，先看「x 范围」判断坐标系：数值接近世界坐标 → 放大半径；\n")
                .append("    数值在 0 附近小幅摆动 → 提交的是相机相对坐标。\n");
        return sb.toString();
    }

    private static String buildJson(Map<String, TypeStat> stats, long elapsedMs) {
        StringBuilder sb = new StringBuilder("{\n");
        sb.append("  \"target\": [").append(fmt(tx)).append(", ").append(fmt(ty)).append(", ")
                .append(fmt(tz)).append("],\n");
        sb.append("  \"camera\": [").append(fmt(camX)).append(", ").append(fmt(camY)).append(", ")
                .append(fmt(camZ)).append("],\n");
        sb.append("  \"radius\": ").append(fmt(radius)).append(",\n");
        sb.append("  \"windowMs\": ").append(elapsedMs).append(",\n");
        sb.append("  \"types\": [\n");
        boolean firstType = true;
        for (TypeStat st : stats.values()) {
            if (!firstType) {
                sb.append(",\n");
            }
            firstType = false;
            sb.append("    {\"renderType\": \"").append(escape(st.name)).append("\", ")
                    .append("\"quads\": ").append(st.quads).append(", ")
                    .append("\"vertices\": ").append(st.vertices).append(", ")
                    .append("\"hitWorld\": ").append(st.hitWorld).append(",\n")
                    .append("     \"quadsInBox\": [");
            boolean firstQuad = true;
            for (Quad q : st.boxQuads) {
                if (!firstQuad) {
                    sb.append(", ");
                }
                firstQuad = false;
                sb.append('[');
                for (int i = 0; i < 4; i++) {
                    if (i > 0) {
                        sb.append(", ");
                    }
                    sb.append('[').append(fmt(q.x[i])).append(", ").append(fmt(q.y[i])).append(", ")
                            .append(fmt(q.z[i])).append(", ").append(fmt(q.u[i])).append(", ")
                            .append(fmt(q.v[i])).append(", ").append(q.light[i]).append(']');
                }
                sb.append(", ").append(q.dir).append(']');
            }
            sb.append("]}");
        }
        sb.append("\n  ]\n}\n");
        return sb.toString();
    }

    private static String fmt(double d) {
        return String.format(Locale.ROOT, "%.4f", d);
    }

    private static String escape(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    // ------------------------------------------------------------ 统计 ----

    /** 单个 RenderType 的累计统计，同时也是该类型的记录入口。 */
    private static final class TypeStat implements QuadSink {
        final String name;
        int quads;
        int vertices;
        int hitWorld;
        double minX = Double.MAX_VALUE;
        double maxX = -Double.MAX_VALUE;
        final List<Quad> boxQuads = new ArrayList<>();
        /** 按方块归属的 sink（flywheel@x,y,z）：收录该方块**全部**几何，不做逐四边形盒子裁剪。 */
        final boolean force;

        TypeStat(String name) {
            this.name = name;
            this.force = name.startsWith("flywheel@");
        }

        boolean hasBounds() {
            return maxX >= minX;
        }

        @Override
        public void quad(float[] xyz12, float[] uv8, int[] light4, float[] nrm12) {
            Quad q = new Quad();
            double mx = 0, my = 0, mz = 0;
            for (int i = 0; i < 4; i++) {
                float x = xyz12[i * 3];
                q.x[i] = x;
                q.y[i] = xyz12[i * 3 + 1];
                q.z[i] = xyz12[i * 3 + 2];
                q.u[i] = uv8[i * 2];
                q.v[i] = uv8[i * 2 + 1];
                q.light[i] = light4[i];
                mx += x / 4.0;
                my += xyz12[i * 3 + 1] / 4.0;
                mz += xyz12[i * 3 + 2] / 4.0;
                if (x < minX) {
                    minX = x;
                }
                if (x > maxX) {
                    maxX = x;
                }
            }
            q.dir = faceDir(nrm12, q);
            quads++;
            // 按方块归属的 sink：方块已在范围内，收录其**全部**几何（含伸出邻格的部分），
            // 不做逐四边形裁剪——否则压在盒子边界的方块会被切掉一半。
            if (force) {
                if (boxQuads.size() < MAX_QUADS_PER_TYPE) {
                    q.hit = 1;
                    boxQuads.add(q);
                }
                return;
            }
            byte hit = classify(mx, my, mz);
            if (hit == 1) {
                hitWorld++;
            }
            if (hit != 0 && boxQuads.size() < MAX_QUADS_PER_TYPE) {
                q.hit = hit;
                boxQuads.add(q);
            }
        }

        /**
         * 面朝向 0..5（与烘焙端同口径：0=+X 1=−X 2=+Y 3=−Y 4=+Z 5=−Z）。
         *
         * <p>优先用顶点法线（渲染器自己给的，最可靠）；法线缺失时退回绕序叉积。
         * 资产包只用一个离散方向做背面判定与明暗，故取**主导轴**。
         */
        private static byte faceDir(float[] nrm, Quad q) {
            double nx = 0, ny = 0, nz = 0;
            for (int i = 0; i < 4; i++) {
                nx += nrm[i * 3];
                ny += nrm[i * 3 + 1];
                nz += nrm[i * 3 + 2];
            }
            if (nx == 0 && ny == 0 && nz == 0) {
                double ux = q.x[1] - q.x[0], uy = q.y[1] - q.y[0], uz = q.z[1] - q.z[0];
                double vx = q.x[2] - q.x[0], vy = q.y[2] - q.y[0], vz = q.z[2] - q.z[0];
                nx = uy * vz - uz * vy;
                ny = uz * vx - ux * vz;
                nz = ux * vy - uy * vx;
            }
            double ax = Math.abs(nx), ay = Math.abs(ny), az = Math.abs(nz);
            if (ax >= ay && ax >= az) {
                return (byte) (nx >= 0 ? 0 : 1);
            }
            if (ay >= az) {
                return (byte) (ny >= 0 ? 2 : 3);
            }
            return (byte) (nz >= 0 ? 4 : 5);
        }
    }

    /** 一条记录下来的四边形（4 顶点：位置 / uv / 光照）。 */
    private static final class Quad {
        final float[] x = new float[4];
        final float[] y = new float[4];
        final float[] z = new float[4];
        final float[] u = new float[4];
        final float[] v = new float[4];
        final int[] light = new int[4];
        byte hit;
        byte dir;                  // 面朝向 0..5（与烘焙端同口径）
    }

    /**
     * 四边形是否落在目标方块附近。
     *
     * @return 0=未命中；1=按**世界坐标**命中；2=按**相机相对坐标**命中（提交坐标 = 世界 − 相机）
     */
    static byte classify(double x, double y, double z) {
        double r = radius;
        if (Math.abs(x - tx) <= r && Math.abs(y - ty) <= r && Math.abs(z - tz) <= r) {
            return 1;
        }
        double wx = x + camX, wy = y + camY, wz = z + camZ;
        if (Math.abs(wx - tx) <= r && Math.abs(wy - ty) <= r && Math.abs(wz - tz) <= r) {
            return 2;
        }
        return 0;
    }
}
