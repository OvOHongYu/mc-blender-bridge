import java.awt.image.BufferedImage;
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.jar.*;
import javax.imageio.ImageIO;

/**
 * 用原版 GrassColors/FoliageColors 计算 colormap 采样真值，供 Python 实现对拍。
 *
 * 用法:
 *   java ProbeColormap <minecraft jar>                 # 自检：4 种像素顺序试验
 *   java ProbeColormap <minecraft jar> <td文件>        # 对拍：每行 "t d" -> 输出 "t d grassRGB foliageRGB"
 *
 * 关键（1.21.1 字节码 + 实测）:
 *   colorMap 由 png 按行主序填充: colorMap[y*256+x] = ARGB(png(x, y))
 *   平原 (t=0.8, d=0.4) -> 91BD59
 */
public class ProbeColormap {

    public static void main(String[] args) throws Exception {
        String jarPath = args[0];
        BufferedImage grassImg, folImg;
        try (JarFile jf = new JarFile(jarPath)) {
            grassImg = ImageIO.read(jf.getInputStream(
                    jf.getJarEntry("assets/minecraft/textures/colormap/grass.png")));
            folImg = ImageIO.read(jf.getInputStream(
                    jf.getJarEntry("assets/minecraft/textures/colormap/foliage.png")));
        }
        Class<?> gc = Class.forName("net.minecraft.world.biome.GrassColors");
        Class<?> fc = Class.forName("net.minecraft.world.biome.FoliageColors");
        Method gSet = gc.getMethod("setColorMap", int[].class);
        Method fSet = fc.getMethod("setColorMap", int[].class);
        Method gGet = gc.getMethod("getColor", double.class, double.class);
        Method fGet = fc.getMethod("getColor", double.class, double.class);

        gSet.invoke(null, (Object) rowMajorArgb(grassImg));
        fSet.invoke(null, (Object) rowMajorArgb(folImg));

        if (args.length < 2) {
            System.out.println("self-check rowMajor-ARGB: (0.8,0.4)=0x"
                    + hex(gGet.invoke(null, 0.8, 0.4)) + " expect 91BD59");
            return;
        }
        StringBuilder sb = new StringBuilder();
        for (String line : Files.readAllLines(Paths.get(args[1]), StandardCharsets.UTF_8)) {
            line = line.trim();
            if (line.isEmpty() || line.startsWith("#")) continue;
            String[] p = line.split("\\s+");
            double t = Double.parseDouble(p[0]);
            double d = Double.parseDouble(p[1]);
            int g = (Integer) gGet.invoke(null, t, d);
            int f = (Integer) fGet.invoke(null, t, d);
            sb.append(String.format("%s %s %06X %06X%n", p[0], p[1],
                    g & 0xFFFFFF, f & 0xFFFFFF));
        }
        System.out.print(sb);
    }

    static int[] rowMajorArgb(BufferedImage img) {
        int w = img.getWidth(), h = img.getHeight();
        int[] arr = new int[65536];
        for (int y = 0; y < h && y < 256; y++) {
            for (int x = 0; x < w && x < 256; x++) {
                arr[y * 256 + x] = img.getRGB(x, y) | 0xFF000000;
            }
        }
        return arr;
    }

    static String hex(Object o) {
        return String.format("%08X", ((Integer) o) & 0xFFFFFFFFL);
    }
}
