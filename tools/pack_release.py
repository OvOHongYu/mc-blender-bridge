# -*- coding: utf-8 -*-
"""打包发行产物（跨平台，CI 与本地共用；ROOT 相对路径）。

产物：
  dist/mc_bridge-<ver>-blender-addon.zip   Blender 插件（mc_bridge/ 顶层目录）
  dist/mc-blender-bridge-<ver>-dist.zip    插件 zip + 模组 jar + INSTALL.txt + bake_assets.py
  dist/INSTALL.txt

用法：python tools/pack_release.py
版本号从 blender_addon/mc_bridge/__init__.py 的 bl_info 与 mcmod/gradle.properties
各读一份并断言一致，避免"改了一边忘另一边"。
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_SRC = os.path.join(ROOT, "blender_addon", "mc_bridge")
MOD_JAR_DIR = os.path.join(ROOT, "mcmod", "build", "libs")
DIST = os.path.join(ROOT, "dist")


def addon_version():
    src = open(os.path.join(ADDON_SRC, "__init__.py"), encoding="utf-8").read()
    m = re.search(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', src)
    if not m:
        sys.exit("bl_info.version 解析失败")
    return ".".join(m.groups())


def mod_version():
    p = os.path.join(ROOT, "mcmod", "gradle.properties")
    m = re.search(r"mod_version\s*=\s*([\d.]+)", open(p, encoding="utf-8").read())
    if not m:
        sys.exit("gradle.properties mod_version 解析失败")
    return m.group(1)


VER = addon_version()
if VER != mod_version():
    sys.exit("版本不一致: bl_info=%s gradle=%s" % (VER, mod_version()))
MOD_JAR = "mcbridge-%s.jar" % VER
ADDON_ZIP = os.path.join(DIST, "mc_bridge-%s-blender-addon.zip" % VER)
DIST_ZIP = os.path.join(DIST, "mc-blender-bridge-%s-dist.zip" % VER)
INSTALL = os.path.join(DIST, "INSTALL.txt")

INSTALL_TXT = """MC Blender Bridge v{ver} 发行包
================================

内容
----
1. mc_bridge-{ver}-blender-addon.zip   Blender 插件（控制模式 + 存档模式 + 资产包）
2. {jar}                Minecraft 1.21.1 Fabric 模组（仅控制模式需要）
3. bake_assets.py                    资产烘焙工具（原版/模组贴图 + 模型，可选但强烈推荐）

安装
----
[Blender 插件]
  Blender -> 编辑 -> 偏好设置 -> 插件 -> 安装 -> 选择
  mc_bridge-{ver}-blender-addon.zip -> 勾选启用。N 面板 -> MC Bridge 即面板。

[MC 模组]（仅「控制模式」需要；存档模式无需任何 MC 组件）
  放入 mods/ 目录，启动后日志出现
  "MC Bridge API 已启动: http://127.0.0.1:8788" 即成功。

[资产包]（可选，强烈推荐：原版+模组贴图与真实模型形状）
  pip install pillow
  python bake_assets.py "<客户端 jar>" "mods/*.jar" -o assets.mcba --mc-version 1.21.1
  然后在 N 面板 -> 资产包 -> 选择 assets.mcba -> 加载。

快速上手
--------
* 控制模式：N 面板 -> 加载模式=控制模式 -> 主机 127.0.0.1 端口 8788 -> 连接。
  「玩家跟随相机」开启时，Blender 相机会实时带动 MC 玩家。
* 存档模式：加载模式=存档模式 -> 选择世界存档目录（含 region/ 与 level.dat）-> 连接。

v{ver} 主要变更
------------
见仓库 docs/roadmap.md 与 Release 页面说明。
""".format(ver=VER, jar=MOD_JAR)


def zip_dir(src, dst):
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                p = os.path.join(base, f)
                rel = os.path.relpath(p, os.path.dirname(src)).replace(os.sep, "/")
                z.write(p, rel)


def main():
    os.makedirs(DIST, exist_ok=True)
    zip_dir(ADDON_SRC, ADDON_ZIP)
    with open(INSTALL, "w", encoding="utf-8-sig", newline="\r\n") as f:
        f.write(INSTALL_TXT)
    jar = os.path.join(MOD_JAR_DIR, MOD_JAR)
    if not os.path.isfile(jar):
        sys.exit("未找到模组 jar: %s（先在 mcmod 下执行 gradle build）" % jar)
    with zipfile.ZipFile(DIST_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ADDON_ZIP, os.path.basename(ADDON_ZIP))
        z.write(jar, MOD_JAR)
        z.write(INSTALL, "INSTALL.txt")
        z.write(os.path.join(ROOT, "tools", "bake_assets.py"), "bake_assets.py")
    print("插件包:", ADDON_ZIP, os.path.getsize(ADDON_ZIP))
    print("发行包:", DIST_ZIP, os.path.getsize(DIST_ZIP))


if __name__ == "__main__":
    main()
