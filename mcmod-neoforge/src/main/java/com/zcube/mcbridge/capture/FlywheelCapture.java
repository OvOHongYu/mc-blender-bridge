package com.zcube.mcbridge.capture;

import dev.engine_room.flywheel.api.instance.InstanceType;
import dev.engine_room.flywheel.api.model.Mesh;
import dev.engine_room.flywheel.api.model.Model;
import dev.engine_room.flywheel.api.vertex.MutableVertexList;
import dev.engine_room.flywheel.lib.instance.InstanceTypes;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.entity.BlockEntity;

import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 客户端专用：从 **Flywheel 的视觉层**直接读取机器几何（R17b）。
 *
 * <p>背景：Create 6 的机关本体（`*Visual`）由 Flywheel 以 **GPU 实例化**渲染，
 * 顶点不经过任何 {@code VertexConsumer}，因此 R17「拦 MultiBufferSource」抓不到。
 * 但 Flywheel 的实例由 `InstancerProvider#instancer(InstanceType, Model, bias)` 创建，
 * **{@code Model} 就是几何源**；创建发生在某个 {@code BlockEntityVisual} 的构造里，
 * 于是能把它**精确归属到方块**。
 *
 * <p>流程：
 * <ol>
 *   <li>{@code BlockEntityStorage#createRaw} 前后置线程局部「当前方块(pos + 状态名)」；</li>
 *   <li>`InstancerProviderImpl#instancer` 出口记录 `(方块, Model, InstanceType)`；</li>
 *   <li>`BaseInstancer`/`IndirectInstancer#createInstance` 出口把实例对象挂到对应
 *       `Model` 上，用于读取**实例变换**（位置/旋转）；</li>
 *   <li>捕获窗口内，遍历登记表里靠近目标的方块，读出网格顶点、施加实例变换，
 *       换算成相机相对坐标，喂进 {@link GeometryCapture} 的既有管线。</li>
 * </ol>
 */
public final class FlywheelCapture {

    /** 模型顶点单位 → 格：0..16 者除以 16；0..1 者直用（由 {@link #scaleOf} 判定）。 */
    private static final float SCALE_16 = 1.0f / 16.0f;

    private static final ThreadLocal<Ctx> CURRENT = new ThreadLocal<>();
    /** 方块 pos(asLong) -> 该方块视觉用到的 (状态名, Model, InstanceType, 实例表)。 */
    private static final Map<Long, List<Entry>> REGISTRY = new ConcurrentHashMap<>();
    /** Instancer 对象(身份) -> (Model, InstanceType)：Instancer 会被 Flywheel 复用，故只映射模型。 */
    private static final Map<Object, Info> INST2INFO =
            Collections.synchronizedMap(new IdentityHashMap<>());

    private static final class Info {
        final Model model;
        final InstanceType<?> type;
        Info(Model m, InstanceType<?> t) { model = m; type = t; }
    }

    /** 本次捕获是否已采集过（避免每 tick 重复采集；一次快照足够）。 */
    private static volatile boolean harvested = false;
    private static volatile boolean scaleLogged = false;
    /** Flywheel 本帧的渲染原点：实例坐标 = 世界 − 渲染原点。 */
    private static volatile double roX, roY, roZ;

    private FlywheelCapture() {
    }

    private static final class Ctx {
        final BlockPos pos;
        final String state;
        Ctx(BlockPos p, String s) { pos = p; state = s; }
    }

    private static final class Entry {
        final String state;
        final Model model;
        final InstanceType<?> type;
        final List<Object> instances = new ArrayList<>();
        Entry(String s, Model m, InstanceType<?> t) { state = s; model = m; type = t; }
    }

    // ------------------------------------------------------------ 登记 ----

    public static void beginBlock(BlockEntity be) {
        try {
            CURRENT.set(new Ctx(be.getBlockPos().immutable(), String.valueOf(be.getBlockState())));
        } catch (Throwable ignored) {
            CURRENT.remove();
        }
    }

    public static void endBlock() {
        CURRENT.remove();
    }

    /** 视觉构造期间创建 instancer：记录 Model 并归属到当前方块；同时登记 instancer->(Model,Type)。 */
    public static void recordInstancer(InstanceType<?> type, Model model, Object instancer) {
        Ctx c = CURRENT.get();
        if (c == null || model == null || type == InstanceTypes.SHADOW) {
            return;
        }
        if (instancer != null) {
            INST2INFO.put(instancer, new Info(model, type));
        }
        entryFor(c.pos, model, type);
    }

    /** `createInstance` 出口：用**当前线程的方块**把实例挂到对应 (方块,Model) 上（Instancer 会被复用）。 */
    public static void noteInstance(Object instancer, Object instance) {
        if (instancer == null || instance == null) {
            return;
        }
        Info info = INST2INFO.get(instancer);
        Ctx c = CURRENT.get();
        if (info == null || c == null) {
            return;
        }
        Entry e = entryFor(c.pos, info.model, info.type);
        if (e != null) {
            synchronized (e.instances) {
                if (e.instances.size() < 512) {
                    e.instances.add(instance);
                }
            }
        }
    }

    private static Entry entryFor(BlockPos pos, Model model, InstanceType<?> type) {
        List<Entry> list = REGISTRY.computeIfAbsent(pos.asLong(), k -> new ArrayList<>());
        synchronized (list) {
            for (Entry e : list) {
                if (e.model == model && e.type == type) {
                    return e;
                }
            }
            Entry e = new Entry(String.valueOf(pos), model, type);
            list.add(e);
            return e;
        }
    }

    // ------------------------------------------------------------ 采集 ----

    public static void tick() {
        if (!GeometryCapture.capturing()) {
            harvested = false;
            return;
        }
        if (harvested) {
            return;
        }
        harvested = true;
        try {
            QuadRecorder.ensureSpriteTable();
            harvest();
        } catch (Throwable t) {
            System.out.println("[mcbridge] Flywheel 采集异常: " + t);
        }
    }

    private static void harvest() {
        readRenderOrigin();
        double[] cam = GeometryCapture.cameraPos();
        double[] box = GeometryCapture.captureBox();
        double tx = box[0], ty = box[1], tz = box[2], r = box[3];

        int blocks = 0;
        int quads = 0;
        for (Map.Entry<Long, List<Entry>> e : REGISTRY.entrySet()) {
            BlockPos p = BlockPos.of(e.getKey());
            if (Math.abs(p.getX() + 0.5 - tx) > r + 2 || Math.abs(p.getY() + 0.5 - ty) > r + 2
                    || Math.abs(p.getZ() + 0.5 - tz) > r + 2) {
                continue;
            }
            // 层名带方块坐标：机器几何会"伸进"相邻方块，导出端据此**直接归属**，
            // 不走按质心重判（否则伸出部分会被丢进空气方块而剔除）。
            GeometryCapture.QuadSink sink = GeometryCapture.sinkFor(
                    "flywheel@" + p.getX() + "," + p.getY() + "," + p.getZ());
            if (sink == null) {
                return;
            }
            List<Entry> list;
            synchronized (e.getValue()) {
                list = new ArrayList<>(e.getValue());
            }
            boolean any = false;
            for (Entry en : list) {
                int n = emitEntry(sink, en, p, cam);
                quads += n;
                any |= n > 0;
            }
            if (any) {
                blocks++;
            }
        }
        System.out.println("[mcbridge] Flywheel 采集完成: 方块 " + blocks + " 个, 四边形 " + quads);
        try {
            dumpDebug(tx, ty, tz, r);
        } catch (Throwable ignored) {
        }
    }

    /** 把目标附近方块的实例变换落盘，供离线核对（user.dir/mcbridge-capture/flywheel-instances.txt）。 */
    private static void dumpDebug(double tx, double ty, double tz, double r) {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<Long, List<Entry>> e : REGISTRY.entrySet()) {
            BlockPos p = BlockPos.of(e.getKey());
            if (Math.abs(p.getX() + 0.5 - tx) > r + 2 || Math.abs(p.getY() + 0.5 - ty) > r + 2
                    || Math.abs(p.getZ() + 0.5 - tz) > r + 2) {
                continue;
            }
            List<Entry> list;
            synchronized (e.getValue()) {
                list = new ArrayList<>(e.getValue());
            }
            for (Entry en : list) {
                List<Object> insts;
                synchronized (en.instances) {
                    insts = new ArrayList<>(en.instances);
                }
                sb.append("block ").append(p).append(" model=")
                        .append(en.model.getClass().getName()).append(" instances=")
                        .append(insts.size()).append('\n');
                for (Model.ConfiguredMesh cm : en.model.meshes()) {
                    Mesh m = cm.mesh();
                    String idx;
                    try {
                        idx = m.indexSequence().getClass().getSimpleName();
                    } catch (Throwable t) {
                        idx = "?";
                    }
                    float u0 = 0, v0 = 0, umin = 1, umax = 0, vmin = 1, vmax = 0;
                    try {
                        int vc = m.vertexCount();
                        if (vc > 0) {
                            Recorder rr = new Recorder(vc);
                            m.write(rr);
                            u0 = rr.u[0];
                            v0 = rr.v[0];
                            for (int vi = 0; vi < vc; vi++) {
                                umin = Math.min(umin, rr.u[vi]);
                                umax = Math.max(umax, rr.u[vi]);
                                vmin = Math.min(vmin, rr.v[vi]);
                                vmax = Math.max(vmax, rr.v[vi]);
                            }
                        }
                    } catch (Throwable ignored) {
                    }
                    String mat = "?";
                    try {
                        mat = String.valueOf(cm.material().texture());
                    } catch (Throwable ignored) {
                    }
                    sb.append("    mesh ").append(m.getClass().getSimpleName())
                            .append(" verts=").append(m.vertexCount())
                            .append(" idx=").append(idx)
                            .append(" quad=").append(isQuad(m))
                            .append(" mat=").append(mat)
                            .append(String.format(java.util.Locale.ROOT,
                                    " uv0=(%.4f,%.4f) u[%.4f..%.4f] v[%.4f..%.4f]",
                                    u0, v0, umin, umax, vmin, vmax))
                            .append('\n');
                }
                for (int i = 0; i < Math.min(insts.size(), 4); i++) {
                    Object in = insts.get(i);
                    sb.append("    ").append(in.getClass().getSimpleName());
                    try {
                        Class<?> c = in.getClass();
                        float ix = c.getField("x").getFloat(in);
                        float iy = c.getField("y").getFloat(in);
                        float iz = c.getField("z").getFloat(in);
                        Object rot = c.getField("rotation").get(in);
                        sb.append(String.format(java.util.Locale.ROOT,
                                " xyz=(%.3f,%.3f,%.3f) rot=%s", ix, iy, iz, rot));
                    } catch (Throwable t1) {
                        try {
                            Object pose = in.getClass().getField("pose").get(in);
                            sb.append(" pose=").append(pose);
                        } catch (Throwable t2) {
                            sb.append(" (no transform fields)");
                        }
                    }
                    sb.append('\n');
                }
            }
        }
        try {
            java.nio.file.Path dir = java.nio.file.Paths.get(
                    System.getProperty("user.dir", "."), "mcbridge-capture");
            java.nio.file.Files.createDirectories(dir);
            java.nio.file.Files.write(dir.resolve("flywheel-instances.txt"),
                    sb.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
        }
    }

    private static int emitEntry(GeometryCapture.QuadSink sink, Entry en, BlockPos p, double[] cam) {
        List<Object> insts;
        synchronized (en.instances) {
            insts = new ArrayList<>(en.instances);
        }
        if (insts.isEmpty()) {
            return emitModel(sink, en.model, null, p, cam);   // 未拿到实例：恒等变换
        }
        int q = 0;
        for (Object inst : insts) {
            q += emitModel(sink, en.model, inst, p, cam);
        }
        return q;
    }

    /** 把一个 Model 的所有网格顶点按 4 顶点一组作为四边形投给 sink，返回四边形数。 */
    private static int emitModel(GeometryCapture.QuadSink sink, Model model, Object inst,
                                 BlockPos p, double[] cam) {
        int quads = 0;
        float[] loc = new float[3];
        Xf xf = readXf(inst, p);
        for (Model.ConfiguredMesh cm : model.meshes()) {
            Mesh mesh = cm.mesh();
            int n = mesh.vertexCount();
            if (n < 3) {
                continue;
            }
            Recorder rec = new Recorder(n);
            mesh.write(rec);
            if (!isQuad(mesh)) {
                continue;
            }
            float sc = scaleOf(rec, n);
            for (int i = 0; i + 3 < n; i += 4) {
                float[] xyz = new float[12];
                float[] uv = new float[8];
                float[] nrm = new float[12];
                int[] light = new int[4];
                org.joml.Vector3f nv = new org.joml.Vector3f();
                for (int k = 0; k < 4; k++) {
                    int vi = i + k;
                    applyXf(xf, rec.x[vi] * sc, rec.y[vi] * sc, rec.z[vi] * sc, p, loc);
                    xyz[k * 3] = (float) (p.getX() + loc[0] - cam[0]);
                    xyz[k * 3 + 1] = (float) (p.getY() + loc[1] - cam[1]);
                    xyz[k * 3 + 2] = (float) (p.getZ() + loc[2] - cam[2]);
                    uv[k * 2] = rec.u[vi];
                    uv[k * 2 + 1] = rec.v[vi];
                    // 法线也要随实例变换：否则烘焙按法线算的面朝向会错，导致背面剔除"只剩一半"
                    applyXn(xf, rec.nx[vi], rec.ny[vi], rec.nz[vi], nv);
                    nrm[k * 3] = nv.x;
                    nrm[k * 3 + 1] = nv.y;
                    nrm[k * 3 + 2] = nv.z;
                    light[k] = 0;
                }
                sink.quad(xyz, uv, light, nrm);
                quads++;
            }
        }
        return quads;
    }

    /** 读 Flywheel 的渲染原点（实例坐标 = 世界 − 渲染原点）。 */
    private static void readRenderOrigin() {
        try {
            net.minecraft.client.Minecraft mc = net.minecraft.client.Minecraft.getInstance();
            if (mc != null && mc.level != null) {
                var mgr = dev.engine_room.flywheel.impl.visualization.VisualizationManagerImpl.get(mc.level);
                if (mgr != null) {
                    var ro = mgr.renderOrigin();
                    roX = ro.getX();
                    roY = ro.getY();
                    roZ = ro.getZ();
                }
            }
        } catch (Throwable ignored) {
        }
    }

    /**
     * 实例变换（读一次，复用）。语义对照 Create/Flywheel 顶点着色器；实例坐标是
     * **「世界 − 渲染原点」**，故换算世界要 **加回渲染原点**：
     * <ul>
     *   <li>RotatingInstance({@code rotating.vert}) / ScrollInstance:
     *       {@code world = R·(v−0.5) + pos + 0.5 (+ 渲染原点)}</li>
     *   <li>TransformedInstance/PosedInstance({@code transformed.vert}):
     *       {@code world = pose·v (+ 渲染原点)}</li>
     * </ul>
     * 用反射读公开字段，避免硬依赖 Create/Flywheel 具体实例类。
     */
    private static final class Xf {
        static final int IDENTITY = 0;
        static final int ROT_CENTER = 1;
        static final int MATRIX = 2;
        int kind = IDENTITY;
        final org.joml.Quaternionf rot = new org.joml.Quaternionf();
        float ox, oy, oz;                  // RotatingInstance 的 pos（渲染原点相对）
        final org.joml.Matrix4f pose = new org.joml.Matrix4f();
    }

    private static Xf readXf(Object inst, BlockPos p) {
        Xf xf = new Xf();
        if (inst == null) {
            return xf;
        }
        Class<?> c = inst.getClass();
        try {
            xf.rot.set((org.joml.Quaternionfc) c.getField("rotation").get(inst));
            xf.ox = c.getField("x").getFloat(inst);
            xf.oy = c.getField("y").getFloat(inst);
            xf.oz = c.getField("z").getFloat(inst);
            xf.kind = Xf.ROT_CENTER;
            return xf;
        } catch (Throwable ignored) {
        }
        try {
            xf.pose.set((org.joml.Matrix4fc) c.getField("pose").get(inst));
            xf.kind = Xf.MATRIX;
        } catch (Throwable ignored) {
        }
        return xf;
    }

    /** 返回**方块局部**坐标（相对方块原点，单位=格）。 */
    private static void applyXf(Xf xf, float vx, float vy, float vz,
                                BlockPos p, float[] out) {
        if (xf.kind == Xf.ROT_CENTER) {
            org.joml.Vector3f t = new org.joml.Vector3f(vx - 0.5f, vy - 0.5f, vz - 0.5f);
            xf.rot.transform(t);
            out[0] = (float) (t.x + xf.ox + roX + 0.5 - p.getX());
            out[1] = (float) (t.y + xf.oy + roY + 0.5 - p.getY());
            out[2] = (float) (t.z + xf.oz + roZ + 0.5 - p.getZ());
        } else if (xf.kind == Xf.MATRIX) {
            org.joml.Vector3f t = new org.joml.Vector3f(vx, vy, vz);
            xf.pose.transformPosition(t);
            out[0] = (float) (t.x + roX - p.getX());
            out[1] = (float) (t.y + roY - p.getY());
            out[2] = (float) (t.z + roZ - p.getZ());
        } else {
            out[0] = vx; out[1] = vy; out[2] = vz;
        }
    }

    /** 法线随实例旋转（只用旋转部分）。 */
    private static void applyXn(Xf xf, float nx, float ny, float nz, org.joml.Vector3f out) {
        out.set(nx, ny, nz);
        if (xf.kind == Xf.ROT_CENTER) {
            xf.rot.transform(out);
        } else if (xf.kind == Xf.MATRIX) {
            xf.pose.transformDirection(out);
        }
    }

    /** 判定网格顶点单位：0..1 者直接用；0..16 者除以 16。以最大绝对坐标做启发式。 */
    private static float scaleOf(Recorder rec, int n) {
        float maxAbs = 0f;
        for (int i = 0; i < n; i++) {
            maxAbs = Math.max(maxAbs, Math.abs(rec.x[i]));
            maxAbs = Math.max(maxAbs, Math.abs(rec.y[i]));
            maxAbs = Math.max(maxAbs, Math.abs(rec.z[i]));
        }
        float sc = maxAbs > 2.0f ? SCALE_16 : 1.0f;
        if (!scaleLogged) {
            scaleLogged = true;
            System.out.println("[mcbridge] Flywheel 网格 maxAbs=" + maxAbs + " 采用缩放=" + sc);
        }
        return sc;
    }

    private static boolean isQuad(Mesh mesh) {
        try {
            return mesh.indexSequence().getClass().getName().contains("Quad");
        } catch (Throwable t) {
            return true;
        }
    }

    // ------------------------------------------------- 顶点列表实现 ----

    private static final class Recorder implements MutableVertexList {
        final int n;
        final float[] x, y, z, u, v, nx, ny, nz;

        Recorder(int n) {
            this.n = n;
            x = new float[n]; y = new float[n]; z = new float[n];
            u = new float[n]; v = new float[n];
            nx = new float[n]; ny = new float[n]; nz = new float[n];
        }

        @Override public void x(int i, float val) { x[i] = val; }
        @Override public void y(int i, float val) { y[i] = val; }
        @Override public void z(int i, float val) { z[i] = val; }
        @Override public void r(int i, float val) { }
        @Override public void g(int i, float val) { }
        @Override public void b(int i, float val) { }
        @Override public void a(int i, float val) { }
        @Override public void u(int i, float val) { u[i] = val; }
        @Override public void v(int i, float val) { v[i] = val; }
        @Override public void overlay(int i, int val) { }
        @Override public void light(int i, int val) { }
        @Override public void normalX(int i, float val) { nx[i] = val; }
        @Override public void normalY(int i, float val) { ny[i] = val; }
        @Override public void normalZ(int i, float val) { nz[i] = val; }

        @Override public int vertexCount() { return n; }
        @Override public float x(int i) { return x[i]; }
        @Override public float y(int i) { return y[i]; }
        @Override public float z(int i) { return z[i]; }
        @Override public float r(int i) { return 1f; }
        @Override public float g(int i) { return 1f; }
        @Override public float b(int i) { return 1f; }
        @Override public float a(int i) { return 1f; }
        @Override public float u(int i) { return u[i]; }
        @Override public float v(int i) { return v[i]; }
        @Override public int overlay(int i) { return 0; }
        @Override public int light(int i) { return 0; }
        @Override public float normalX(int i) { return nx[i]; }
        @Override public float normalY(int i) { return ny[i]; }
        @Override public float normalZ(int i) { return nz[i]; }
    }
}
