import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.joml.Vector3f;

/**
 * 从 Minecraft（yarn 映射）jar 中提取原版**实体**模型的**分层**几何。
 *
 * 与 DumpModels.java 的区别：方块实体的变换（facing 等）是固定属性，导出时
 * 直接烘焙进顶点即可；而盔甲架等实体的部件旋转（Pose NBT）是**运行时**任意
 * 角度，必须保留"每个部件自己的 pivot / 默认旋转 / 本地顶点"，运行时再装配。
 *
 * 输出（tools/vanilla_entity_models.json）:
 *   { "models": { "<key>": {"texW", "texH", "parts": [
 *       {"name": "<路径>", "pivot": [x,y,z], "rot": [pitch,yaw,roll],
 *        "cuboids": [{"faces": [{"n": [nx,ny,nz], "v": [[x,y,z,u,v]...]}]}],
 *        "children": [...]}
 *   ]}} }
 * 顶点为**部件局部坐标**（1/16 方块单位，以该部件 pivot 为原点，未变换）；
 * 运行时变换 = 父矩阵 · T(pivot) · Rz(roll) · Ry(yaw) · Rx(pitch)。
 *
 * 用法: java DumpEntityModels <输出 json 路径>
 */
public class DumpEntityModels {

    /** 模型键 -> {类名, 静态工厂方法, 纹理宽, 纹理高}。 */
    static final String[][] FACTORIES = {
        {"armor_stand", "net.minecraft.client.render.entity.model.ArmorStandEntityModel",
                "getTexturedModelData", "64", "64"},
    };

    static final StringBuilder OUT = new StringBuilder();
    static Class<?> MODEL_PART;
    static Field F_CUBOIDS, F_CHILDREN, F_SIDES;

    public static void main(String[] args) throws Exception {
        String outPath = args[0];
        MODEL_PART = Class.forName("net.minecraft.client.model.ModelPart");
        Class<?> cuboidCls = Class.forName("net.minecraft.client.model.ModelPart$Cuboid");
        Class<?> quadCls = Class.forName("net.minecraft.client.model.ModelPart$Quad");
        Class<?> vertexCls = Class.forName("net.minecraft.client.model.ModelPart$Vertex");
        F_CUBOIDS = MODEL_PART.getDeclaredField("cuboids");
        F_CHILDREN = MODEL_PART.getDeclaredField("children");
        F_SIDES = cuboidCls.getDeclaredField("sides");
        F_CUBOIDS.setAccessible(true);
        F_CHILDREN.setAccessible(true);
        F_SIDES.setAccessible(true);
        Field fQuadVerts = quadCls.getField("vertices");
        Field fQuadDir = quadCls.getField("direction");
        Field fVertPos = vertexCls.getField("pos");
        Field fVertU = vertexCls.getField("u");
        Field fVertV = vertexCls.getField("v");

        OUT.append("{\n  \"models\": {\n");
        boolean first = true;
        for (String[] spec : FACTORIES) {
            String key = spec[0];
            Object tmd;
            try {
                tmd = buildModel(spec);
            } catch (Throwable t) {
                System.err.println("skip " + key + ": " + t);
                continue;
            }
            if (tmd == null) { System.err.println("skip " + key + ": null"); continue; }
            Field fDim = tmd.getClass().getDeclaredField("dimensions");
            fDim.setAccessible(true);
            Object dim = fDim.get(tmd);
            Field fW = dim.getClass().getDeclaredField("width");
            Field fH = dim.getClass().getDeclaredField("height");
            fW.setAccessible(true);
            fH.setAccessible(true);
            int texW = (Integer) fW.get(dim);
            int texH = (Integer) fH.get(dim);
            Object part = tmd.getClass().getMethod("createModel").invoke(tmd);

            if (!first) OUT.append(",\n");
            first = false;
            OUT.append("    \"").append(key).append("\": {\"texW\": ").append(texW)
               .append(", \"texH\": ").append(texH).append(", \"parts\": [");
            dumpPartLocal(part, "", fQuadVerts, fQuadDir, fVertPos, fVertU, fVertV);
            OUT.append("\n    ]}");
            System.err.println("ok " + key + " tex=" + texW + "x" + texH);
        }
        OUT.append("\n  }\n}\n");

        try (Writer wr = new OutputStreamWriter(new FileOutputStream(outPath), StandardCharsets.UTF_8)) {
            wr.write(OUT.toString());
        }
        System.out.println("dumped " + FACTORIES.length + " entity model specs");
    }

    /** 调用静态工厂；返回 TexturedModelData。 */
    static Object buildModel(String[] spec) throws Exception {
        Class<?> cls = Class.forName(spec[1]);
        Class<?> texData = Class.forName("net.minecraft.client.model.TexturedModelData");
        Method m = null;
        for (Method cand : cls.getMethods()) {
            if (!cand.getName().equals(spec[2])) continue;
            if (cand.getParameterCount() == 0) { m = cand; break; }
        }
        if (m == null) return null;
        Object res = m.invoke(null);
        if (texData.isInstance(res)) return res;
        int w = Integer.parseInt(spec[3]);
        int h = Integer.parseInt(spec[4]);
        return texData.getMethod("of", Class.forName("net.minecraft.client.model.ModelData"), int.class, int.class)
                     .invoke(null, res, w, h);
    }

    /** 分层导出：不应用矩阵，输出 pivot / 默认旋转 / 本地顶点。 */
    static void dumpPartLocal(Object part, String path,
                              Field fQuadVerts, Field fQuadDir, Field fVertPos,
                              Field fVertU, Field fVertV) throws Exception {
        Object t = MODEL_PART.getMethod("getDefaultTransform").invoke(part);
        Class<?> tc = t.getClass();
        float px = tc.getField("pivotX").getFloat(t);
        float py = tc.getField("pivotY").getFloat(t);
        float pz = tc.getField("pivotZ").getFloat(t);
        float pitch = tc.getField("pitch").getFloat(t);
        float yaw = tc.getField("yaw").getFloat(t);
        float roll = tc.getField("roll").getFloat(t);

        String name = path.isEmpty() ? "root" : path;
        OUT.append("\n      {\"name\": \"").append(name).append("\", \"pivot\": [")
           .append(fmt(px)).append(",").append(fmt(py)).append(",").append(fmt(pz))
           .append("], \"rot\": [").append(fmt(pitch)).append(",").append(fmt(yaw))
           .append(",").append(fmt(roll)).append("], \"cuboids\": [");
        List<?> cuboids = (List<?>) F_CUBOIDS.get(part);
        boolean firstC = true;
        for (Object cub : cuboids) {
            if (!firstC) OUT.append(", ");
            firstC = false;
            Object[] sides = (Object[]) F_SIDES.get(cub);
            OUT.append("{\"faces\": [");
            boolean firstQ = true;
            for (Object quad : sides) {
                if (quad == null) continue;
                Object[] verts = (Object[]) fQuadVerts.get(quad);
                Vector3f dir = new Vector3f((Vector3f) fQuadDir.get(quad));
                if (!firstQ) OUT.append(", ");
                firstQ = false;
                OUT.append("{\"n\": [")
                   .append(fmt(dir.x)).append(",").append(fmt(dir.y)).append(",").append(fmt(dir.z))
                   .append("], \"v\": [");
                for (int i = 0; i < verts.length; i++) {
                    Object v = verts[i];
                    Vector3f pos = new Vector3f((Vector3f) fVertPos.get(v));
                    float u = fVertU.getFloat(v);
                    float vv = fVertV.getFloat(v);
                    if (i > 0) OUT.append(", ");
                    OUT.append("[").append(fmt(pos.x)).append(",").append(fmt(pos.y))
                       .append(",").append(fmt(pos.z)).append(",")
                       .append(fmt(u)).append(",").append(fmt(vv)).append("]");
                }
                OUT.append("]}");
            }
            OUT.append("]}");
        }
        OUT.append("]");
        Map<?, ?> children = (Map<?, ?>) F_CHILDREN.get(part);
        if (!children.isEmpty()) {
            OUT.append(", \"children\": [");
            boolean firstCh = true;
            for (Map.Entry<?, ?> e : children.entrySet()) {
                if (!firstCh) OUT.append(",");
                firstCh = false;
                String childPath = path.isEmpty() ? (String) e.getKey() : path + "/" + e.getKey();
                dumpPartLocal(e.getValue(), childPath, fQuadVerts, fQuadDir, fVertPos, fVertU, fVertV);
            }
            OUT.append("]");
        }
        OUT.append("}");
    }

    static String fmt(float f) {
        if (f == (long) f) return Long.toString((long) f);
        String s = String.format(Locale.ROOT, "%.5f", f);
        while (s.endsWith("0")) s = s.substring(0, s.length() - 1);
        if (s.endsWith(".")) s = s.substring(0, s.length() - 1);
        return s;
    }
}
