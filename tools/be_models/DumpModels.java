import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.joml.Matrix4f;
import org.joml.Vector3f;

/**
 * 从 Minecraft（yarn 映射）jar 中提取原版方块实体模型的精确几何。
 *
 * 原版方块实体（箱子/床/告示牌/旗帜/潜影盒/头颅/传送门框架…）没有 JSON 几何，
 * 其形状由 Java 的 TexturedModelData 定义。本工具直接调用原版模型工厂，
 * 把烘焙后的顶点/UV/面法线导出为 JSON，供 tools/bake_assets.py 使用。
 *
 * 用法: java DumpModels <输出 json 路径>
 */
public class DumpModels {

    /** 模型键 -> {类名, 静态工厂方法, 纹理宽, 纹理高}；宽高为 0 时从 TexturedModelData 读取。 */
    static final String[][] FACTORIES = {
        {"chest", "net.minecraft.client.render.block.entity.ChestBlockEntityRenderer", "getSingleTexturedModelData", "0", "0"},
        {"chest_left", "net.minecraft.client.render.block.entity.ChestBlockEntityRenderer", "getLeftDoubleTexturedModelData", "0", "0"},
        {"chest_right", "net.minecraft.client.render.block.entity.ChestBlockEntityRenderer", "getRightDoubleTexturedModelData", "0", "0"},
        {"bed_head", "net.minecraft.client.render.block.entity.BedBlockEntityRenderer", "getHeadTexturedModelData", "0", "0"},
        {"bed_foot", "net.minecraft.client.render.block.entity.BedBlockEntityRenderer", "getFootTexturedModelData", "0", "0"},
        {"sign", "net.minecraft.client.render.block.entity.SignBlockEntityRenderer", "getTexturedModelData", "0", "0"},
        {"hanging_sign", "net.minecraft.client.render.block.entity.HangingSignBlockEntityRenderer", "getTexturedModelData", "0", "0"},
        {"banner", "net.minecraft.client.render.block.entity.BannerBlockEntityRenderer", "getTexturedModelData", "0", "0"},
        {"shulker_box", "net.minecraft.client.render.entity.model.ShulkerEntityModel", "getTexturedModelData", "0", "0"},
        {"skull_head", "net.minecraft.client.render.entity.model.SkullEntityModel", "getHeadTexturedModelData", "0", "0"},
        {"skull_floor", "net.minecraft.client.render.entity.model.SkullEntityModel", "getSkullTexturedModelData", "0", "0"},
        {"dragon_head", "net.minecraft.client.render.entity.model.DragonHeadEntityModel", "getTexturedModelData", "0", "0"},
        {"piglin_head", "net.minecraft.client.render.entity.model.PiglinHeadEntityModel", "getModelData", "64", "64"},
        {"player_head", "net.minecraft.client.render.entity.model.PlayerEntityModel", "getTexturedModelData", "64", "64"},
        {"conduit_shell", "net.minecraft.client.render.block.entity.ConduitBlockEntityRenderer", "getShellTexturedModelData", "0", "0"},
        {"conduit_plain", "net.minecraft.client.render.block.entity.ConduitBlockEntityRenderer", "getPlainTexturedModelData", "0", "0"},
        {"conduit_eye", "net.minecraft.client.render.block.entity.ConduitBlockEntityRenderer", "getEyeTexturedModelData", "0", "0"},
        {"conduit_wind", "net.minecraft.client.render.block.entity.ConduitBlockEntityRenderer", "getWindTexturedModelData", "0", "0"},
        {"decorated_pot_body", "net.minecraft.client.render.block.entity.DecoratedPotBlockEntityRenderer", "getTopBottomNeckTexturedModelData", "0", "0"},
        {"decorated_pot_sides", "net.minecraft.client.render.block.entity.DecoratedPotBlockEntityRenderer", "getSidesTexturedModelData", "0", "0"},
        {"bell", "net.minecraft.client.render.block.entity.BellBlockEntityRenderer", "getTexturedModelData", "0", "0"},
        {"book", "net.minecraft.client.render.entity.model.BookModel", "getTexturedModelData", "0", "0"},
    };

    static final String[] TEXTURE_CLASSES = {
        "net.minecraft.client.render.block.entity.ChestBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.BannerBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.BedBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.BellBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.ConduitBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.DecoratedPotBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.HangingSignBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.ShulkerBoxBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.SignBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.SkullBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.CampfireBlockEntityRenderer",
        "net.minecraft.client.render.block.entity.EnchantingTableBlockEntityRenderer",
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
            dumpPart(part, new Matrix4f(), "", fQuadVerts, fQuadDir, fVertPos, fVertU, fVertV);
            OUT.append("\n    ]}");
            System.err.println("ok " + key + " tex=" + texW + "x" + texH);
        }
        OUT.append("\n  },\n");

        OUT.append("  \"textures\": {\n");
        boolean firstTex = true;
        for (String rn : TEXTURE_CLASSES) {
            Class<?> rc;
            try { rc = Class.forName(rn); } catch (Throwable t) { continue; }
            String simple = rn.substring(rn.lastIndexOf('.') + 1);
            for (Field f : rc.getDeclaredFields()) {
                if (!Modifier.isStatic(f.getModifiers())) continue;
                String tn = f.getType().getName();
                boolean isId = tn.endsWith("Identifier");
                boolean isSprite = tn.endsWith("SpriteIdentifier");
                if (!isId && !isSprite) continue;
                f.setAccessible(true);
                Object val = f.get(null);
                if (val == null) continue;
                String s;
                try {
                    s = isSprite ? String.valueOf(val.getClass().getMethod("getTextureId").invoke(val))
                                 : String.valueOf(val);
                } catch (Throwable t) { continue; }
                if (!firstTex) OUT.append(",\n");
                firstTex = false;
                OUT.append("    \"").append(simple).append(".").append(f.getName())
                   .append("\": \"").append(s).append("\"");
            }
        }
        OUT.append("\n  }\n}\n");

        try (Writer wr = new OutputStreamWriter(new FileOutputStream(outPath), StandardCharsets.UTF_8)) {
            wr.write(OUT.toString());
        }
        System.out.println("dumped " + FACTORIES.length + " model specs");
    }

    /** 调用静态工厂；返回 TexturedModelData（ModelData 工厂自动包装）。 */
    static Object buildModel(String[] spec) throws Exception {
        Class<?> cls = Class.forName(spec[1]);
        Class<?> texData = Class.forName("net.minecraft.client.model.TexturedModelData");
        Method m = null;
        for (Method cand : cls.getMethods()) {
            if (!cand.getName().equals(spec[2])) continue;
            if (cand.getParameterCount() == 0) { m = cand; break; }
            if (cand.getParameterCount() == 2 && keyNeedsDilation(spec[0])) { m = cand; break; }
        }
        if (m == null) return null;
        Object res;
        if (m.getParameterCount() == 0) {
            res = m.invoke(null);
        } else {
            Class<?> dilation = Class.forName("net.minecraft.client.model.Dilation");
            Object none = dilation.getField("NONE").get(null);
            boolean slim = spec[0].contains("slim");
            res = m.invoke(null, none, slim);
        }
        if (texData.isInstance(res)) return res;
        int w = Integer.parseInt(spec[3]);
        int h = Integer.parseInt(spec[4]);
        return texData.getMethod("of", Class.forName("net.minecraft.client.model.ModelData"), int.class, int.class)
                     .invoke(null, res, w, h);
    }

    static boolean keyNeedsDilation(String key) { return key.startsWith("player_head"); }

    static void dumpPart(Object part, Matrix4f parentM, String path,
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

        // 注意：ModelPart.Cuboid 的顶点与 pivot 均为 1/16 方块单位
        // （渲染时才统一 *0.0625），因此全程保持 1/16 单位。
        Matrix4f m = new Matrix4f(parentM);
        m.translate(px, py, pz);
        if (roll != 0f) m.rotateZ(roll);
        if (yaw != 0f) m.rotateY(yaw);
        if (pitch != 0f) m.rotateX(pitch);

        String name = path.isEmpty() ? "root" : path;
        OUT.append("\n      {\"name\": \"").append(name).append("\", \"cuboids\": [");
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
                // 法线只受旋转/镜像影响（m 的 3x3 部分）
                float nx = m.m00() * dir.x + m.m10() * dir.y + m.m20() * dir.z;
                float ny = m.m01() * dir.x + m.m11() * dir.y + m.m21() * dir.z;
                float nz = m.m02() * dir.x + m.m12() * dir.y + m.m22() * dir.z;
                float nl = (float) Math.sqrt(nx * nx + ny * ny + nz * nz);
                if (nl > 1e-6f) { nx /= nl; ny /= nl; nz /= nl; }
                if (!firstQ) OUT.append(", ");
                firstQ = false;
                OUT.append("{\"n\": [")
                   .append(fmt(nx)).append(",").append(fmt(ny)).append(",").append(fmt(nz))
                   .append("], \"v\": [");
                for (int i = 0; i < verts.length; i++) {
                    Object v = verts[i];
                    Vector3f pos = new Vector3f((Vector3f) fVertPos.get(v));
                    m.transformPosition(pos);
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
                dumpPart(e.getValue(), m, childPath, fQuadVerts, fQuadDir, fVertPos, fVertU, fVertV);
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
