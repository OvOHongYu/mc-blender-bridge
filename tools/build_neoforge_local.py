# -*- coding: utf-8 -*-
"""mcmod-neoforge 的本地构建脚本（GFW 网络环境用）。

背景：mcmod-neoforge 的正规构建走 ModDevGradle（需要访问 maven.neoforged.net
下载 neoform-runtime / neoform 等工具链）。CI（GitHub Actions）网络可达，直接
`gradle build` 即可；但本地网络对 maven.neoforged.net 连接重置且无公共镜像，
因此本脚本改用 **本机 .minecraft 实例自带的工具链** 完成等价构建：

  1. 从实例 libraries 取 vanilla slim jar（混淆名）与 Mojang 官方映射；
  2. 用实例自带的 AutoRenamingTool（--reverse）重命名为 mojmap 作编译类路径；
  3. javac 编译 mcmod 共享源码（排除 5 个 Fabric 专属文件）+ mcmod-neoforge 源码；
  4. 打包为 build/libs/mcbridge-neoforge-<ver>.jar（与 gradle 产物同路径同内容）。

产物与 MDG 构建一致：编译类路径即玩家运行时同名类（mojmap），mods.toml/mixins.json
随包。版本号仍以 mcmod/gradle.properties 为单一来源。

用法：
  python tools/build_neoforge_local.py --instance "E:\\.minecraft\\versions\\1.21.1-NeoForge_21.1.256"
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NFP = os.path.join(ROOT, "mcmod-neoforge")
FABRIC_SRC = os.path.join(ROOT, "mcmod", "src", "main", "java")
NF_SRC = os.path.join(NFP, "src", "main", "java")
NF_RES = os.path.join(NFP, "src", "main", "resources")

# 与 mcmod-neoforge/build.gradle 的 exclude 完全一致（Fabric 专属文件）
EXCLUDES = {
    "com/zcube/mcbridge/McBridge.java",
    "com/zcube/mcbridge/mixin/WorldChunkMixin.java",
    "com/zcube/mcbridge/core/ChunkSnapshotService.java",
    "com/zcube/mcbridge/http/ApiServer.java",
    "com/zcube/mcbridge/mesh/BlockClassifier.java",
}


def mod_version():
    p = os.path.join(ROOT, "mcmod", "gradle.properties")
    m = re.search(r"mod_version\s*=\s*([\d.]+)", open(p, encoding="utf-8").read())
    if not m:
        sys.exit("gradle.properties mod_version 解析失败")
    return m.group(1)


def find_one(pattern, what):
    def vkey(p):
        nums = re.findall(r"\d+", os.path.basename(p))
        return [int(n) for n in nums]
    hits = sorted(glob.glob(pattern), key=vkey)
    if not hits:
        sys.exit("未找到 %s: %s" % (what, pattern))
    return hits[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True,
                    help=".minecraft 实例版本目录（含 version json）")
    ap.add_argument("--libraries", default=None,
                    help="库目录（默认推断 <instance>/../../libraries）")
    args = ap.parse_args()
    inst = args.instance
    libs = args.libraries or os.path.normpath(os.path.join(inst, "..", "..", "libraries"))
    if not os.path.isdir(libs):
        sys.exit("实例 libraries 不存在: %s" % libs)

    ver = mod_version()
    work = os.path.join(NFP, "build", "tmp")
    out_libs = os.path.join(NFP, "build", "libs")
    os.makedirs(work, exist_ok=True)
    os.makedirs(out_libs, exist_ok=True)

    # ---- 1. vanilla slim jar + 官方映射（neoforge 安装时生成） ----
    slim = find_one(os.path.join(libs, "net", "minecraft", "client",
                                 "1.21.1-*", "client-*-slim.jar"), "vanilla slim jar")
    mappings = find_one(os.path.join(libs, "net", "minecraft", "client",
                                     "1.21.1-*", "client-*-mappings.txt"), "官方映射")
    print("slim:", os.path.basename(slim))

    # ---- 2. ART 重命名为 mojmap ----
    art = find_one(os.path.join(libs, "net", "neoforged", "AutoRenamingTool",
                                "*", "AutoRenamingTool-*-all.jar"), "AutoRenamingTool")
    mc_jar = os.path.join(work, "mc-1.21.1-mojmap.jar")
    if os.path.exists(mc_jar):
        os.remove(mc_jar)
    subprocess.check_call([
        "java", "-jar", art,
        "--input", slim, "--output", mc_jar, "--map", mappings, "--reverse",
        "--ann-fix", "--ids-fix", "--src-fix", "--record-fix",
    ])
    # 抽查重命名确实生效（防止 ART 未来版本行为变化）
    with zipfile.ZipFile(mc_jar) as z:
        names = set(z.namelist())
    assert "net/minecraft/world/level/chunk/LevelChunk.class" in names, "mojmap 重命名未生效"
    print("mojmap 类路径就绪:", mc_jar)

    # ---- 3. 编译类路径（从 version json 精确取版本，防止串用其他实例的库） ----
    json_path = os.path.join(inst, os.path.basename(inst.rstrip("/\\")) + ".json")
    vjson = json.load(open(json_path, encoding="utf-8"))
    lib_names = [l.get("name", "") for l in vjson.get("libraries", [])]

    def coords_to_path(name):
        """group:artifact:version[:classifier] -> 库文件路径"""
        parts = name.split(":")
        group, artifact, version = parts[0], parts[1], parts[2]
        classifier = parts[3] if len(parts) > 3 else None
        base = os.path.join(libs, *group.split("."), artifact, version,
                            "%s-%s" % (artifact, version) + (("-" + classifier) if classifier else "") + ".jar")
        if not os.path.isfile(base):
            sys.exit("库文件缺失: %s" % base)
        return base

    def pick(name_selector, what, fallback_glob=None):
        hits = [n for n in lib_names if name_selector in n]
        if hits:
            return coords_to_path(hits[0])
        if fallback_glob:
            g = sorted(glob.glob(os.path.join(libs, *fallback_glob)))
            if g:
                return g[-1]
        sys.exit("version json 中未找到 %s" % what)

    # neoforge universal 不在 version json 库列表里（FML 引导按 --fml.neoForgeVersion
    # 自行装载），因此从游戏参数解析版本号后直接定位。
    game_args = " ".join(a for a in (vjson.get("arguments", {}).get("game") or []) if isinstance(a, str))
    m_nf = re.search(r"--fml\.neoForgeVersion\s+(\S+)", game_args) or re.search(r"NeoForge[ _](\d+\.\d+\.\d+)", os.path.basename(inst))
    if not m_nf:
        sys.exit("无法从 version json / 实例目录解析 NeoForge 版本")
    nf_ver = m_nf.group(1)
    universal = os.path.join(libs, "net", "neoforged", "neoforge", nf_ver,
                             "neoforge-%s-universal.jar" % nf_ver)
    if not os.path.isfile(universal):
        sys.exit("neoforge universal 缺失: %s" % universal)

    cp = [
        mc_jar,
        universal,
        pick("fancymodloader:loader:", "fml loader"),
        pick("net.neoforged:bus:", "neoforged bus"),
        pick("slf4j-api:", "slf4j-api"),
        # Registry 接口继承 com.mojang.serialization.Keyable（DFU），
        # StateDefinition.getPossibleStates() 返回 ImmutableList（guava），编译需要
        find_one(os.path.join(libs, "com", "mojang", "datafixerupper", "*", "datafixerupper-*.jar"),
                 "datafixerupper"),
        find_one(os.path.join(libs, "com", "google", "guava", "guava", "*", "guava-*.jar"), "guava"),
        # 命令 API（客户端 /mcbcap 用到）依赖 brigadier，它是独立库不在客户端 jar 内
        find_one(os.path.join(libs, "com", "mojang", "brigadier", "*", "brigadier-*.jar"),
                 "brigadier"),
        # mixin 由 fmlloader JarJar 携带，不在 version json 里：取本地最高 0.8.x
        find_one(os.path.join(libs, "org", "spongepowered", "mixin", "*", "mixin-*.jar"),
                 "mixin"),
        # joml：Flywheel 实例变换（Matrix4f/Quaternionf/Vector3f）编译需要
        find_one(os.path.join(libs, "org", "joml", "joml", "*", "joml-*.jar"), "joml"),
    ]

    # Flywheel（随 Create 以 jarjar 内嵌）用于编译 Flywheel 侧捕获代码（R17b）：
    # 从实例 mods 里的 create-*.jar 抽出 META-INF/jarjar/flywheel-*.jar 落到 work。
    create_jars = sorted(glob.glob(os.path.join(inst, "mods", "create-*.jar")))
    if create_jars:
        with zipfile.ZipFile(create_jars[-1]) as cz:
            nested = [n for n in cz.namelist()
                      if n.startswith("META-INF/jarjar/flywheel-") and n.endswith(".jar")]
            if nested:
                fw_jar = os.path.join(work, "flywheel-compile.jar")
                with open(fw_jar, "wb") as f:
                    f.write(cz.read(nested[0]))
                cp.append(fw_jar)
                cp.append(create_jars[-1])      # Create 本体也在类路径上，便于引用
                print("flywheel 编译类路径:", os.path.basename(fw_jar))

    # ---- 4. 源码清单（共享 + 本工程，排除 Fabric 专属） ----
    sources = []
    for base, is_nf in ((FABRIC_SRC, False), (NF_SRC, True)):
        for dirpath, _, files in os.walk(base):
            for f in sorted(files):
                if not f.endswith(".java"):
                    continue
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                if not is_nf and rel in EXCLUDES:
                    continue
                sources.append(full)
    # javac argfile 中反斜杠是转义符：一律写正斜杠并用引号包住空格路径
    sources_file = os.path.join(work, "sources.txt")
    with open(sources_file, "w", encoding="utf-8") as f:
        f.write("\n".join('"%s"' % s.replace(os.sep, "/") for s in sources))

    classes = os.path.join(NFP, "build", "classes", "java", "main")
    if os.path.exists(classes):
        shutil.rmtree(classes)
    os.makedirs(classes)
    subprocess.check_call([
        "javac", "-proc:none",  # mojmap 运行时无需 refmap；mixin AP 会因缺 gson 崩溃
        "-J-Duser.language=en", "-J-Duser.country=US",   # 报错用英文，避免中文乱码
        "-encoding", "UTF-8", "--release", "21",
        "-cp", os.pathsep.join(cp), "-d", classes,
        "@" + sources_file.replace(os.sep, "/"),
    ])
    print("javac 完成: %d 个源文件" % len(sources))

    # ---- 5. 打包（资源 + 展开版本号） ----
    jar_path = os.path.join(out_libs, "mcbridge-neoforge-%s.jar" % ver)
    with zipfile.ZipFile(jar_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, files in os.walk(classes):
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                z.write(full, os.path.relpath(full, classes).replace(os.sep, "/"))
        for dirpath, _, files in os.walk(NF_RES):
            for f in sorted(files):
                full = os.path.join(dirpath, f)
                arc = os.path.relpath(full, NF_RES).replace(os.sep, "/")
                if arc == "META-INF/neoforge.mods.toml":
                    text = open(full, encoding="utf-8").read()
                    text = text.replace("${version}", ver)
                    z.writestr(arc, text)
                else:
                    z.write(full, arc)
    print("打包完成:", jar_path, os.path.getsize(jar_path))


if __name__ == "__main__":
    main()
