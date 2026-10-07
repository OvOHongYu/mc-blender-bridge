# -*- coding: utf-8 -*-
"""通用工具：区块坐标、日志、PNG 写出（模拟服务器与测试用）。"""
import logging
import struct
import zlib

LOG = logging.getLogger("mc_bridge")


def setup_logging(level=logging.INFO):
    logging.basicConfig(level=level, format="[%(levelname)s %(name)s] %(message)s")


def chunk_of(x: float, z: float):
    return int(x) >> 4, int(z) >> 4


def chunk_origin(cx: int, cz: int):
    return cx << 4, cz << 4


# ---- 区块组名单序列化（R10/R11：常见区块 / 更新白黑名单，随 .blend 保存）----

def parse_group_list(s):
    """'dim:gx,gz;dim:gx,gz' -> [(dim, gx, gz), ...]；坏条目跳过。"""
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        dim, coords = part.rsplit(":", 1)
        try:
            gx, gz = (int(v) for v in coords.split(","))
        except ValueError:
            continue
        out.append((dim, gx, gz))
    return out


def serialize_group_list(keys):
    """[(dim, gx, gz), ...] -> 'dim:gx,gz;...'（保持顺序，去重）。"""
    out, seen = [], set()
    for dim, gx, gz in keys or ():
        k = (str(dim), int(gx), int(gz))
        if k in seen:
            continue
        seen.add(k)
        out.append("%s:%d,%d" % k)
    return ";".join(out)


def png_bytes(width, height, rgba: bytearray) -> bytes:
    """极简 PNG 编码（RGBA8，无依赖），返回字节。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def write_png(path, width, height, rgba: bytearray):
    """极简 PNG 写出（RGBA8，无依赖），返回字节。"""
    png = png_bytes(width, height, rgba)
    with open(path, "wb") as f:
        f.write(png)
    return png
