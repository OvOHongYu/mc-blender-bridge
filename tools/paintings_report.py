#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""画实体定位对拍报告（R5 待办：画的位置未与原版逐块对拍）。

用法:
  python tools/paintings_report.py <存档目录> [维度] [--assets assets.mcba] [--out report.json]

遍历 `<维度>/entities/r.*.mca` 的全部画实体，输出原始 NBT 字段
（id / variant / facing / Pos，以及旧版本才有的 TileX/TileY/TileZ），
和按 `core/entities.py` 约定计算的几何（尺寸 / 朝向 / 底边中点 / 画面中心），
供与游戏画面逐块核对：

- 在真实存档里放置若干不同尺寸/朝向的画，运行本工具；
- 对照游戏（F3 或实体坐标）确认每幅画的 **Pos 语义**（底边中点 vs 角块）
  与 **facing 朝向**是否与报告一致；
- 发现偏差时把该行截图 + 报告条目发给项目，修正 `core/entities.py`
  （1.21.4 及以前另有 TileX/TileY/TileZ、1.21.5+ 改为 block_pos，语义有差，
  见 Mojira MC-295759）。

无 --assets 时只输出 NBT 原始字段（尺寸/朝向留空）。"""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))

from mc_bridge.core.anvil import NBTReader, RegionFile, norm_dim  # noqa: E402

_DIM_DIR = {"minecraft:overworld": "", "minecraft:the_nether": "DIM-1",
            "minecraft:the_end": "DIM1"}


def _ents_in_file(reg, lx, lz):
    data = reg.chunk_bytes(lx, lz)
    if not data:
        return []
    named = NBTReader(data).read_named()
    root = named[1] if named else {}
    return root.get("Entities") or []


def main(argv=None):
    ap = argparse.ArgumentParser(description="画实体定位对拍报告")
    ap.add_argument("save", help="MC 存档目录")
    ap.add_argument("dim", nargs="?", default="minecraft:overworld")
    ap.add_argument("--assets", default=None, help="MCBA1 资产包（画尺寸/贴图）")
    ap.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = ap.parse_args(argv)

    dim = norm_dim(args.dim)
    base = args.save if not _DIM_DIR.get(dim) else os.path.join(args.save, _DIM_DIR[dim])
    edir = os.path.join(base, "entities")
    if not os.path.isdir(edir):
        raise SystemExit("找不到实体目录: %s" % edir)

    pack = None
    if args.assets:
        sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
        from mc_bridge.core import assets as A
        pack = A.AssetPack.load(args.assets)

    rows = []
    for f in sorted(glob.glob(os.path.join(edir, "r.*.*.mca"))):
        name = os.path.basename(f)
        rx, rz = (int(name.split(".")[1]), int(name.split(".")[2]))
        reg = RegionFile(f)
        try:
            for lx in range(32):
                for lz in range(32):
                    for ent in _ents_in_file(reg, lx, lz):
                        if str(ent.get("id") or "") not in ("minecraft:painting", "painting"):
                            continue
                        cx, cz = rx * 32 + lx, rz * 32 + lz
                        row = {"chunk": [cx, cz], "variant": ent.get("variant"),
                               "facing": ent.get("facing"), "Pos": ent.get("Pos"),
                               "Tile": [ent.get(k) for k in ("TileX", "TileY", "TileZ")
                                        if k in ent]}
                        if pack and row["variant"]:
                            info = pack.paintings.get(str(row["variant"]))
                            if info:
                                w, h, tid = info
                                row["size"] = [w, h]
                                pos = row["Pos"]
                                fwd = ((0, 0, 1), (-1, 0, 0), (0, 0, -1), (1, 0, 0))[
                                    int(row["facing"] or 0) % 4]
                                g = 1.0 / 32.0
                                row["computed"] = {
                                    "bottom_center": [pos[0], pos[1], pos[2]],
                                    "center": [pos[0] + fwd[0] * g, pos[1],
                                               pos[2] + fwd[2] * g],
                                    "normal": list(fwd),
                                    "tex": pack.tex_names[tid],
                                }
                        rows.append(row)
        finally:
            reg.close()

    report = {"dim": dim, "count": len(rows), "paintings": rows}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("已写入 %s（%d 幅画）" % (args.out, len(rows)))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
