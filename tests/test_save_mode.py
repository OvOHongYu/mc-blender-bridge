# -*- coding: utf-8 -*-
"""存档模式测试：NBT/Region 解析、SaveClient、调度器动态装卸、
插件根空物体组织与相机子物体化。全部在无 Blender 环境中验证。"""
import gzip
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, HERE)

import fake_bpy  # noqa: E402
bpy = fake_bpy.install()


# ------------------------------------------------------------ NBT 编码器（测试夹具） ----

def _nb(tag, name, payload):
    return bytes([tag]) + struct.pack(">H", len(name)) + name.encode() + payload


def _byte(v):
    return struct.pack(">b", v)


def _int(v):
    return struct.pack(">i", v)


def _long(v):
    if v >= 1 << 63:
        v -= 1 << 64
    return struct.pack(">q", v)


def _double(v):
    return struct.pack(">d", v)


def _float(v):
    return struct.pack(">f", v)


def _string(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def _longarray(vals):
    return struct.pack(">i", len(vals)) + b"".join(_long(int(v)) for v in vals)


def _list(etag, items):
    return bytes([etag]) + struct.pack(">i", len(items)) + b"".join(items)


def _compound(items):
    return b"".join(_nb(tag, k, v) for tag, k, v in items) + b"\x00"


def _pack_indices(indices, bits):
    """4096 个索引 -> MC 1.16+ PalettedContainer 位流。

    条目不跨 long 边界：每 long 存 floor(64/bits) 个条目（LSB 起），
    高位剩余位补零。"""
    n = 4096
    vpl = 64 // bits
    nlong = (n + vpl - 1) // vpl
    out = np.zeros(nlong, np.uint64)
    ii = np.arange(n)
    vals = np.asarray(indices, np.uint64)
    li = ii // vpl
    sh = ((ii % vpl) * bits).astype(np.uint64)
    np.bitwise_or.at(out, li, vals << sh)
    return out.tolist()


def _palette_compound(names):
    """真实存档格式的 palette：每项是 compound {Name, Properties}。

    注意：list 元素的类型在 list 头声明一次，元素本身**不带类型字节**，
    compound 元素直接写字段 + END。
    名字可带 "minecraft:water[level=4]" 形式的状态后缀（拆进 Properties）。"""
    items = []
    for n in names:
        i = n.find("[")
        base, prop = (n[:i], n[i + 1:-1]) if i >= 0 else (n, "")
        pairs = [kv.split("=", 1) for kv in prop.split(",") if "=" in kv]
        props = [(8, k, _string(v)) for k, v in sorted(pairs)]
        items.append(_compound([(8, "Name", _string(base)),
                                (10, "Properties", _compound(props))]))
    return _list(10, items)


def _chunk_nbt(cx, cz, secs):
    """secs: list[(world_y, palette_names, indices|None)]"""
    sections = []
    for world_y, pal, idx in secs:
        bs = [(9, "palette", _palette_compound(pal))]
        if idx is not None and len(pal) > 1:
            # bits 精确按 palette 大小推导（覆盖 5/6/7 等不整除 64 的场景）
            bits = max(4, (len(pal) - 1).bit_length())
            bs.append((12, "data", _longarray(_pack_indices(idx, bits))))
        st = [(1, "Y", _byte(world_y // 16)),
              (10, "block_states", _compound(bs))]
        sections.append(_compound(st))
    root = _compound([
        (3, "xPos", _int(cx)),
        (3, "zPos", _int(cz)),
        (8, "Status", _string("minecraft:full")),
        (9, "sections", _list(10, sections)),
    ])
    return _nb(10, "", root)


# 20 项 palette（bits=5，条目不整除 64 的回归场景）
_RICH_NAMES = ["minecraft:air", "minecraft:stone", "minecraft:dirt",
               "minecraft:sand", "minecraft:gravel", "minecraft:oak_log",
               "minecraft:oak_planks", "minecraft:stone_bricks",
               "minecraft:coal_ore", "minecraft:iron_ore", "minecraft:bedrock",
               "minecraft:glass", "minecraft:water", "minecraft:oak_leaves",
               "minecraft:poppy", "minecraft:grass_block",
               "minecraft:oak_stairs", "minecraft:sandstone",
               "minecraft:gold_ore", "minecraft:diamond_ore"]


def _rich_indices():
    """y=80 section 的散布索引：每 (x,z) 列按 (x*7+z*3)%19 放一个非空方块。"""
    idx = np.zeros(4096, np.int64)
    for z in range(16):
        for x in range(16):
            v = (x * 7 + z * 3) % 19
            if v == 0:
                continue
            y = (x + z) % 8
            idx[(y << 8) | (z << 4) | x] = v
    return idx


def _make_chunk(cx, cz):
    """3×3 测试世界区块：石头地层 + 地表草地 + 中央柱子/楼梯 +
    20 项 palette 的富区块层（bits=5 打包回归）。"""
    idx = np.zeros(4096, np.int64)
    all_xz = (np.arange(16)[:, None] * 16 + np.arange(16)[None, :]).ravel()
    idx[(0 << 8) | all_xz] = 1
    pal = ["minecraft:air", "minecraft:grass_block"]
    if (cx, cz) == (0, 0):
        pal += ["minecraft:oak_planks", "minecraft:oak_stairs"]
        for ly in range(1, 5):
            idx[(ly << 8) | (3 * 16 + 3)] = 2
        idx[(5 << 8) | (3 * 16 + 3)] = 3
    return _chunk_nbt(cx, cz, [
        (-64, ["minecraft:stone"], None),
        (48, ["minecraft:stone"], None),
        (64, pal, idx),
        (80, _RICH_NAMES, _rich_indices()),
    ])


def _write_region(path, chunks):
    """chunks: dict[(lx, lz)] -> NBT bytes；单 region r.0.0.mca。"""
    header = bytearray(8192)
    entries = sorted(chunks.items())
    nsec_by = {}
    running = 2
    for (lx, lz), nbt in entries:
        comp = zlib.compress(nbt)
        nsec = (5 + len(comp) + 4095) // 4096
        nsec_by[(lx, lz)] = nsec
        running += nsec
    body = bytearray()
    for i, ((lx, lz), nbt) in enumerate(entries):
        comp = zlib.compress(nbt)
        data = struct.pack(">I", len(comp) + 1) + b"\x02" + comp
        nsec = nsec_by[(lx, lz)]
        data += b"\x00" * (nsec * 4096 - len(data))
        idx = (lz & 31) * 32 + (lx & 31)
        header[idx * 4:idx * 4 + 3] = (2 + sum(nsec_by[e[0]] for e in entries[:i])).to_bytes(3, "big")
        header[idx * 4 + 3] = nsec
        header[4096 + idx * 4:4096 + idx * 4 + 4] = (1).to_bytes(4, "big")
        body += data
    with open(path, "wb") as f:
        f.write(bytes(header))
        f.write(bytes(body))


def _level_dat():
    return gzip.compress(_nb(10, "", _compound([
        (3, "DataVersion", _int(3465)),
        (10, "Data", _compound([])),
    ])))


# ------------------------------------------------------------ 实体夹具 ----

def _painting_entity(x, y, z, variant="minecraft:kebab", facing=0):
    """画实体：id / facing / variant / Pos（底边中点，见 core/entities.py）。"""
    return _compound([
        (8, "id", _string("minecraft:painting")),
        (1, "facing", _byte(facing)),
        (8, "variant", _string(variant)),
        (9, "Pos", _list(6, [_double(x), _double(y), _double(z)])),
    ])


def _armor_stand_entity(x, y, z, pose=None):
    """盔甲架实体：id / Pos / 可选 Pose（度数）与部件显隐标记。"""
    items = [
        (8, "id", _string("minecraft:armor_stand")),
        (9, "Pos", _list(6, [_double(x), _double(y), _double(z)])),
        (9, "Rotation", _list(5, [_float(0.0), _float(0.0)])),
    ]
    if pose:
        items.append((10, "Pose", _compound(
            [(8, name, _list(5, [_float(a) for a in ang])) for name, ang in pose.items()])))
    return _compound(items)


def _entity_chunk(ents):
    """实体区域文件里的一个区块：根 compound 含 Entities 列表。"""
    return _nb(10, "", _compound([(9, "Entities", _list(10, ents))]))


def _build_entities(world, ents):
    """写入 entities/r.0.0.mca（区块 (0,0)），实体与方块分开存放。"""
    d = os.path.join(world, "entities")
    os.makedirs(d, exist_ok=True)
    _write_region(os.path.join(d, "r.0.0.mca"), {(0, 0): _entity_chunk(ents)})


def _build_world(tmp):
    world = os.path.join(tmp, "TestWorld")
    os.makedirs(os.path.join(world, "region"), exist_ok=True)
    chunks = {}
    for cx in range(-1, 2):
        for cz in range(-1, 2):
            chunks[(cx & 31, cz & 31)] = _make_chunk(cx, cz)
    _write_region(os.path.join(world, "region", "r.0.0.mca"), chunks)
    with open(os.path.join(world, "level.dat"), "wb") as f:
        f.write(_level_dat())
    # 区块 (0,0) 挂一幅 2×1 的画（贴在 z=8 朝南的墙面上，位置需落在本区块）
    # 与一个默认姿态的盔甲架
    _build_entities(world, [_painting_entity(8.5, 70.0, 8.0,
                                             variant="minecraft:test_wide"),
                             _armor_stand_entity(4.5, 72.0, 4.5)])
    return world


# ------------------------------------------------------------ 解析层 ----

class TestAnvilParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.world_dir = _build_world(cls.tmp)
        from mc_bridge.core.anvil import AnvilWorld, SaveClient, NBTReader
        cls.AnvilWorld, cls.SaveClient, cls.NBTReader = AnvilWorld, SaveClient, NBTReader

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_nbt_roundtrip(self):
        nbt = _nb(10, "", _compound([
            (3, "Int", _int(-42)),
            (1, "Byte", _byte(7)),
            (8, "Str", _string("hello")),
            (12, "Longs", _longarray([1, 2 ** 40, 2 ** 63])),
            (9, "Lst", _list(3, [_int(1), _int(2)])),
        ]))
        name, root = self.NBTReader(nbt).read_named()
        self.assertEqual(name, "")
        self.assertEqual(root["Int"], -42)
        self.assertEqual(root["Byte"], 7)
        self.assertEqual(root["Str"], "hello")
        np.testing.assert_array_equal(root["Longs"], np.array([1, 2 ** 40, 2 ** 63], np.uint64))
        self.assertEqual(root["Lst"], [1, 2])

    def test_region_roundtrip(self):
        from mc_bridge.core.anvil import RegionFile
        reg = RegionFile(os.path.join(self.world_dir, "region", "r.0.0.mca"))
        nbt = reg.chunk_bytes(0, 0)
        self.assertIsNotNone(nbt)
        name, root = self.NBTReader(nbt).read_named()
        secs = root["sections"]
        ys = [s["Y"] for s in secs]
        self.assertEqual(ys, [-4, 3, 4, 5])
        reg.close()

    def test_save_client_chunk(self):
        client = self.SaveClient(self.AnvilWorld(self.world_dir))
        pl = client.chunk("minecraft:overworld", 0, 0, -64, 320)
        self.assertEqual(pl["yBottom"], -64)
        self.assertEqual(len(pl["sections"]), 24)
        sec = pl["sections"][8]                 # y 64..79
        self.assertIsNotNone(sec)
        names = [n for _, n in sec["palette"]]
        self.assertIn("minecraft:grass_block", names)
        self.assertIn("minecraft:oak_stairs", names)
        self.assertEqual(sec["indices"].shape, (4096,))
        self.assertGreater(int(sec["indices"].max()), 0)
        # 反"大立方体"回归：indices 必须同时含 air(0) 与实心方块，而非全同值
        self.assertEqual(int(sec["indices"].min()), 0)
        self.assertGreater(int(sec["indices"].max()), 0)
        # 富区块层（20 项 palette，bits=5 不整除 64）：逐位往返必须一致
        rich = pl["sections"][9]                # y=80..95
        self.assertIsNotNone(rich)
        rich_names = [n for _, n in rich["palette"]]
        self.assertEqual(rich_names, _RICH_NAMES)
        np.testing.assert_array_equal(rich["indices"], _rich_indices())
        # 世界边缘（无 region 文件）-> 全空 payload
        empty = client.chunk("minecraft:overworld", 100, 100, -64, 320)
        self.assertEqual(len(empty["sections"]), 24)
        self.assertTrue(all(s is None for s in empty["sections"]))
        client.close()

    def test_save_client_mesh(self):
        client = self.SaveClient(self.AnvilWorld(self.world_dir))
        m = client.mesh("minecraft:overworld", 0, 0, -64, 320, lod=0)
        self.assertGreater(m["verts"].shape[0], 0)
        self.assertTrue(np.all(m["dirs"] < 6))
        # LOD2 高度壳：有效网格且面数有上界（顶面合并 + 边界裙边，不会按单元爆炸）
        m2 = client.mesh("minecraft:overworld", 0, 0, -64, 320, lod=2)
        self.assertGreater(m2["verts"].shape[0], 0)
        self.assertLess(m2["verts"].shape[0], 16 * 16 * 8)
        # 程序化贴图
        png = client.texture_png("minecraft:stone", "side")
        self.assertEqual(png[:4], b"\x89PNG")
        client.close()

    def test_unpack_both_packings(self):
        """1.16+（不跨 long）与 1.15-（连续）两种位打包的随机往返。"""
        from mc_bridge.core.anvil import _unpack_indices
        rng = np.random.default_rng(42)
        n = 4096
        ii = np.arange(n)
        for bits in (4, 5, 6, 7, 8, 9, 12, 15, 16):
            truth = rng.integers(0, min(1 << bits, 300), n).astype(np.uint64)
            # ---- 1.16+: 每 long vpl 个条目，高位补零 ----
            vpl = 64 // bits
            packed = np.zeros((n + vpl - 1) // vpl, np.uint64)
            np.bitwise_or.at(packed, ii // vpl,
                             truth << ((ii % vpl) * bits).astype(np.uint64))
            np.testing.assert_array_equal(
                _unpack_indices(packed, bits), truth, err_msg=f"span bits={bits}")
            # ---- 1.15-: 连续位流 ----
            flat = np.zeros(n * bits, bool)
            jj = np.arange(bits, dtype=np.uint64)
            pos = (ii[:, None] * bits + jj[None, :].astype(np.int64)).ravel()
            flat[pos] = ((truth[:, None] >> jj[None, :]) & 1).ravel().astype(bool)
            cont = np.zeros((n * bits + 63) // 64, np.uint64)
            for k in range(len(cont)):
                seg = flat[k * 64:(k + 1) * 64]
                cont[k] = sum(int(x) << i for i, x in enumerate(seg))
            np.testing.assert_array_equal(
                _unpack_indices(cont, bits, contiguous=True), truth,
                err_msg=f"contiguous bits={bits}")

    def test_ping_version(self):
        client = self.SaveClient(self.AnvilWorld(self.world_dir))
        info = client.ping()
        self.assertEqual(info["mod"], "mcbridge-save")
        self.assertEqual(info["mcVersion"], "3465")
        self.assertEqual(client.versions("minecraft:overworld", -1, -1, 1, 1)[(0, 0)], 0)
        # 维度短名归一化
        pl = client.chunk("overworld", 0, 0, -64, 320)
        self.assertEqual(pl["dim"], "minecraft:overworld")
        client.close()

    def test_entities(self):
        """实体与方块分开存放：entities/r.x.z.mca 的 Entities 段。"""
        client = self.SaveClient(self.AnvilWorld(self.world_dir))
        ents = client.entities("minecraft:overworld", 0, 0)
        self.assertEqual(len(ents), 2)
        ids = {e["id"] for e in ents}
        self.assertEqual(ids, {"minecraft:painting", "minecraft:armor_stand"})
        painting = next(e for e in ents if e["id"] == "minecraft:painting")
        self.assertEqual(painting["variant"], "minecraft:test_wide")
        self.assertEqual(painting["facing"], 0)
        np.testing.assert_allclose(painting["Pos"], [8.5, 70.0, 8.0])
        # 无实体文件的区块 -> 空列表（不抛异常）
        self.assertEqual(client.entities("minecraft:overworld", 5, 5), [])
        client.close()


# ------------------------------------------------------------ 调度器 ----

class FakeImporter:
    """收集调度器输出，模拟 Blender 主线程。"""

    def __init__(self):
        self.objects = {}

    def apply(self, scheduler, max_loops=400):
        for _ in range(max_loops):
            scheduler.drain()
            applied = scheduler.poll_apply(64)
            for key, payload in applied:
                self.objects[key] = payload
            evicted = scheduler.poll_evict(64)
            for key, _reason in evicted:
                self.objects.pop(key, None)
            if not applied and not evicted:
                if not scheduler.ready and not scheduler.queue \
                        and not scheduler.inflight_keys:
                    return True
            time.sleep(0.02)
        return False


class _FakePaintingPack:
    """最小资产包替身：画变体 + 合成盔甲架分层模型（供实体几何端到端测试）。"""

    paintings = {"minecraft:test_wide": (2, 1, 7)}
    _ARMOR = {
        "texW": 64, "texH": 64, "tex": "minecraft:entity/armorstand/wood",
        "parts": [{
            "name": "root", "pivot": [0, 0, 0], "rot": [0, 0, 0],
            "cuboids": [],
            "children": [
                {"name": "body", "pivot": [0, 0, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, 0, 0, 0, 0], [1, 0, 0, 1, 0], [1, 3, 0, 1, 1], [-1, 3, 0, 0, 1]]}]}]},
                {"name": "head", "pivot": [0, 1, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, -7, 0, 0, 0], [1, -7, 0, 1, 0], [1, 0, 0, 1, 1], [-1, 0, 0, 0, 1]]}]}]},
                {"name": "left_arm", "pivot": [5, 2, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, -2, 0, 0, 0], [1, -2, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
                {"name": "right_arm", "pivot": [-5, 2, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, -2, 0, 0, 0], [1, -2, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
                {"name": "base_plate", "pivot": [0, 12, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-6, 11, 0, 0, 0], [6, 11, 0, 1, 0], [6, 12, 0, 1, 1], [-6, 12, 0, 0, 1]]}]}]},
                {"name": "left_body_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, 3, 0, 0, 0], [1, 3, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
                {"name": "right_body_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-1, 3, 0, 0, 0], [1, 3, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
                {"name": "shoulder_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
                 "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                     [-6, 10, 0, 0, 0], [6, 10, 0, 1, 0], [6, 12, 0, 1, 1], [-6, 12, 0, 0, 1]]}]}]},
            ],
        }],
    }
    entity_models = {"armor_stand": _ARMOR}
    blockstates = {}

    def tex_id(self, name):
        return {"minecraft:entity/armorstand/wood": 8}.get(name)

    def use_model(self, block):
        return False

    def variant_indices(self, block, props=None):
        return []

    def tint_mask(self, block):
        return None

    def classify(self, block):
        return None


class TestSaveScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.world_dir = _build_world(cls.tmp)
        from mc_bridge.core.anvil import AnvilWorld, SaveClient
        from mc_bridge.core.scheduler import Params, Scheduler
        cls.AnvilWorld, cls.SaveClient = AnvilWorld, SaveClient
        cls.Params, cls.Scheduler = Params, Scheduler

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _scheduler(self, **kw):
        base = dict(dim="minecraft:overworld", r_load=1, r_unload=2, lod1_dist=2,
                    lod2_dist=3, ymin=-64, ymax=320, inflight=2, mode="raw")
        base.update(kw)
        world = self.AnvilWorld(self.world_dir)
        client = self.SaveClient(world)
        return self.Scheduler(client, self.Params(**base)), client

    def test_dynamic_load_and_evict(self):
        sch, client = self._scheduler()
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)              # 区块 (0,0)
        self.assertTrue(imp.apply(sch))
        self.assertIn(("minecraft:overworld", 0, 0), imp.objects)
        geo = imp.objects[("minecraft:overworld", 0, 0)]["geo"]
        self.assertGreater(geo["nq"], 0)
        # 远跳 -> 旧区块卸载
        sch.update_anchor(8000.0, 8000.0)
        imp.apply(sch)
        self.assertNotIn(("minecraft:overworld", 0, 0), imp.objects)
        sch.stop()
        client.close()

    def test_painting_merged_into_chunk_geo(self):
        """R5：存档实体（画）合并进区块对象几何，材质走 ("tex", 贴图 id)。"""
        from mc_bridge.core import assets as A
        A.set_global(_FakePaintingPack())
        try:
            sch, client = self._scheduler()      # mode="raw" -> 本地网格路径
            imp = FakeImporter()
            sch.update_anchor(8.0, 8.0)
            ok = imp.apply(sch)
            self.assertEqual(sch.stats["errors"], 0, sch.stats.get("last_error"))
            self.assertTrue(ok)
            geo = imp.objects[("minecraft:overworld", 0, 0)]["geo"]
            slot = geo["mats"].index(("tex", 7))
            sel = np.repeat(geo["mat_idx"] == slot, 4)
            verts = np.asarray(geo["verts"])[sel]
            self.assertEqual(len(verts), 4)                       # 画 = 一个四边形
            # 组局部坐标：底边中点 (8.5, 70, 8) + 沿 +Z 外移 1/32，宽 2 高 1
            self.assertAlmostEqual(float(verts[:, 0].min()), 7.5)
            self.assertAlmostEqual(float(verts[:, 0].max()), 9.5)
            self.assertAlmostEqual(float(verts[:, 1].min()), 70.0 - (-64))
            self.assertAlmostEqual(float(verts[:, 1].max()), 71.0 - (-64))
            for z in verts[:, 2]:
                self.assertAlmostEqual(float(z), 8.0 + 1.0 / 32.0, places=5)
            sch.stop()
            client.close()
        finally:
            A.clear()

    def test_armor_stand_merged_into_chunk_geo(self):
        """R5：盔甲架按 Pose 装配（合成模型），并入区块对象几何。

        默认姿态（无 ShowArms）：躯干 + 头 + 两根木杆 + 肩杆 + 底座 = 6 面，
        底边对齐实体 Pos.y，头在顶部（总高 = 合成模型 30/16 = 1.875）。"""
        from mc_bridge.core import assets as A
        A.set_global(_FakePaintingPack())
        try:
            sch, client = self._scheduler()
            imp = FakeImporter()
            sch.update_anchor(8.0, 8.0)
            ok = imp.apply(sch)
            self.assertEqual(sch.stats["errors"], 0, sch.stats.get("last_error"))
            self.assertTrue(ok)
            geo = imp.objects[("minecraft:overworld", 0, 0)]["geo"]
            slot = geo["mats"].index(("tex", 8))
            sel = np.repeat(geo["mat_idx"] == slot, 4)
            verts = np.asarray(geo["verts"])[sel].reshape(-1, 4, 3)
            self.assertEqual(len(verts), 6)                       # 6 个部件面
            # 底边 = 实体 Pos.y = 72（局部 y = 72 - yBottom）
            self.assertAlmostEqual(float(verts[:, :, 1].min()), 72.0 + 64.0, places=4)
            # 总高 = 合成模型 30/16 = 1.875；头部面在顶部
            self.assertAlmostEqual(float(verts[:, :, 1].max()) - float(verts[:, :, 1].min()),
                                   1.875, places=4)
            sch.stop()
            client.close()
        finally:
            A.clear()

    def test_versions_static(self):
        """存档模式版本恒 0，不触发重载。"""
        sch, client = self._scheduler()
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        key = ("minecraft:overworld", 0, 0)
        sch.maybe_poll_versions(force=True)
        deadline = time.time() + 5
        while sch._version_polling and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sch.state[key]["status"], "LIVE")
        # 组内逐区块版本聚合为元组（组边长 2 -> 4 个区块）
        self.assertEqual(set(sch.state[key]["version"]), {0})
        sch.stop()
        client.close()


# ------------------------------------------------------------ 插件（fake-bpy） ----

class TestSaveModeAddon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for m in [k for k in sys.modules if k == "mc_bridge" or k.startswith("mc_bridge.")]:
            del sys.modules[m]
        import mc_bridge
        mc_bridge.register()
        cls.mc_bridge = mc_bridge
        from mc_bridge import ops, importer, state
        cls.ops, cls.importer, cls.state = ops, importer, state

    @classmethod
    def tearDownClass(cls):
        try:
            cls.mc_bridge.unregister()
        except Exception:
            pass

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.world_dir = _build_world(self.tmp)
        for o in list(bpy.data.objects):
            bpy.data.objects.remove(o)
        for m in list(bpy.data.meshes):
            bpy.data.meshes.remove(m)
        self._set_props(bpy.context.scene.mcb)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_props(self, p):
        p.load_mode = "save"
        p.save_dir = self.world_dir
        p.dim = "minecraft:overworld"
        p.r_load, p.r_unload = 1, 2
        p.lod1_dist, p.lod2_dist = 2, 3
        p.ymin, p.ymax = -64, 320
        p.inflight = 2
        p.apply_per_tick, p.evict_per_tick = 8, 32
        p.poll_interval = 0.02
        p.version_poll = 5.0
        p.auto_frame = True
        p.auto_bind_camera = True

    def test_connect_root_and_camera(self):
        p = bpy.context.scene.mcb
        cam = bpy.data.objects.new("Camera")
        bpy.context.scene.camera = cam
        import fake_bpy as _fb
        cam._matrix_world.translation = _fb._Vec3(8.0, 8.0, 8.0)
        ret = fake_bpy.call_operator("mcb.connect", bpy.context)
        self.assertEqual(ret, {'FINISHED'})
        self.assertIsNotNone(self.state.scheduler())
        # 根空物体存在，相机已自动挂载
        root = bpy.data.objects.get(self.importer.ROOT_NAME)
        self.assertIsNotNone(root)
        self.assertIs(cam.parent, root)
        # 主循环推进 -> 区块对象挂载到根
        sch = self.state.scheduler()
        for _ in range(400):
            self.ops.step_tick(p, bpy.context.scene)
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(self.importer.count_live(), 1)
        obj = bpy.data.objects.get("MCB_ow_0_0")
        self.assertIsNotNone(obj)
        self.assertIs(obj.parent, root)
        self.assertEqual(obj.location[2], -64)
        # 卸载全部不应删除根
        fake_bpy.call_operator("mcb.disconnect", bpy.context)
        self.importer.unload_all()
        self.assertIsNotNone(bpy.data.objects.get(self.importer.ROOT_NAME))
        self.assertEqual(self.importer.count_live(), 0)

# ------------------------------------------------ 存档模式：流体几何（端到端） ----

def _fluid_chunk(cx, cz):
    """0 层 stone 地板；1..2 层水源池（多层 -> 侧面必须整格）；x=7,z=7 三层瀑布柱；
    x=9..13 的 level=4 流动水池（水位高度必须来自 level 属性）。"""
    pal = ["minecraft:air", "minecraft:stone", "minecraft:water[level=0]",
           "minecraft:water[level=4]"]
    if (cx, cz) != (0, 0):
        return _chunk_nbt(cx, cz, [(0, ["minecraft:air"], None)])
    idx = np.zeros(4096, np.int64)
    for z in range(16):
        for x in range(16):
            idx[(0 << 8) | (z << 4) | x] = 1                       # 地板
    for z in range(1, 6):
        for x in range(1, 6):
            idx[(1 << 8) | (z << 4) | x] = 2                       # 水源池（下）
            idx[(2 << 8) | (z << 4) | x] = 2                       # 水源池（上）
    for ly in (1, 2, 3):
        idx[(ly << 8) | (7 << 4) | 7] = 2                          # 瀑布柱
    for z in range(1, 6):
        for x in range(9, 14):
            idx[(1 << 8) | (z << 4) | x] = 3                       # level=4 水池
    return _chunk_nbt(cx, cz, [(0, pal, idx)])


def _build_fluid_world(tmp):
    world = os.path.join(tmp, "FluidWorld")
    os.makedirs(os.path.join(world, "region"), exist_ok=True)
    chunks = {}
    for cx in range(-1, 2):
        for cz in range(-1, 2):
            chunks[(cx & 31, cz & 31)] = _fluid_chunk(cx, cz)
    _write_region(os.path.join(world, "region", "r.0.0.mca"), chunks)
    with open(os.path.join(world, "level.dat"), "wb") as f:
        f.write(_level_dat())
    return world


class TestSaveModeFluidGeometry(unittest.TestCase):
    """存档模式流体几何：解析 -> payload -> 本地网格，四角高度必须符合原版。

    回归：液体方块「上方仍是同种流体」时原版四角 = 1.0（整格竖直侧面），
    曾按加权平均收成 0.8333，导致瀑布/多层水体侧面出现锯齿与缝隙。"""

    LY = 64                 # 世界 y -> payload 局部 y（yBottom = -64）

    @classmethod
    def setUpClass(cls):
        from mc_bridge.core import blocks as _B
        from mc_bridge.core import mesher as _mesher
        from mc_bridge.core.anvil import AnvilWorld, SaveClient
        cls.B, cls.mesher = _B, _mesher
        cls.tmp = tempfile.mkdtemp()
        cls.world_dir = _build_fluid_world(cls.tmp)
        cls.client = SaveClient(AnvilWorld(cls.world_dir))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _mesh(self):
        payloads = {}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                payloads[(dx, dz)] = self.client.chunk("minecraft:overworld", dx, dz, -64, 320)
        quads, pal, _, _ = self.mesher.mesh_payload(payloads, fluids=True)
        return quads, pal

    def _block_at(self, payloads, x, wy, z):
        """区块内 (x, 世界y, z) 的方块状态名（不在本区块/空气则返回 air）。"""
        pl = payloads[(0, 0)]
        si = (wy - pl["yBottom"]) // 16
        if si < 0 or si >= len(pl["sections"]) or not pl["sections"][si]:
            return "minecraft:air"
        s = pl["sections"][si]
        ly = (wy - pl["yBottom"]) % 16
        return s["palette"][int(s["indices"][(ly << 8) | (z << 4) | x])][1]

    def test_deep_liquid_sides_are_full_cells(self):
        """上方仍是同种液体的方块，侧面顶边必须整格（原版四角 = 1.0）。

        回归：曾按加权平均收成 0.8333，瀑布柱与多层水体池壁出现锯齿/缝隙。"""
        payloads = {(dx, dz): self.client.chunk("minecraft:overworld", dx, dz, -64, 320)
                    for dx in (-1, 0, 1) for dz in (-1, 0, 1)}
        quads, pal, _, _ = self.mesher.mesh_payload(payloads, fluids=True)
        liq = {i for i, (c, _) in enumerate(pal) if c == self.B.LIQUID}
        checked = 0
        for verts, d, blk, ao in quads:
            if int(blk) not in liq or d not in (0, 1, 4, 5):
                continue
            X = min(int(v[0]) for v in verts) - (1 if d == 0 else 0)
            Z = min(int(v[2]) for v in verts) - (1 if d == 4 else 0)
            base = min(float(v[1]) for v in verts)
            wy = int(round(base)) - self.LY
            if wy < 0 or wy >= 320 or not (0 <= X < 16 and 0 <= Z < 16):
                continue
            here = self.B.base_name(self._block_at(payloads, X, wy, Z))
            above = self.B.base_name(self._block_at(payloads, X, wy + 1, Z))
            if here != above or here not in ("minecraft:water", "minecraft:lava"):
                continue
            # 侧面顶边两个顶点：原版四角均为 1.0（= base + 1.0），不允许 0.8333 收边
            tops = [float(v[1]) for v in verts if float(v[1]) > base + 1e-6]
            self.assertTrue(tops, "侧面缺少顶边顶点: %s" % (verts,))
            for y in tops:
                self.assertAlmostEqual(y - base, 1.0, places=5,
                                       msg="pos=(%d,%d,%d) dir=%d 侧面顶边 %.4f" % (X, wy, Z, d, y - base))
            checked += 1
        self.assertGreater(checked, 8, "未覆盖到足够的多层液体侧面")

    def test_surface_heights_from_level_property(self):
        quads, pal = self._mesh()
        liq = {i for i, (c, _) in enumerate(pal) if c == self.B.LIQUID}
        tops = [q for q in quads if int(q[2]) in liq and q[1] == 2]

        def surface_at(x, z, ly):
            hit = [q for q in tops
                   if min(v[0] for v in q[0]) == x and min(v[2] for v in q[0]) == z
                   and abs(min(v[1] for v in q[0]) - (ly + self.LY)) < 1.0]
            self.assertEqual(len(hit), 1, "x=%d z=%d ly=%d" % (x, z, ly))
            return [float(v[1]) for v in hit[0][0]]

        # 水源池上层内部格：四角 8/9（水源高度），且必须低于格顶
        for y in surface_at(3, 3, 2):
            self.assertAlmostEqual(y, 2 + self.LY + 8.0 / 9.0, places=5)
        # level=4 流动水池内部格：四角 4/9（水位来自存档 level 属性）
        for y in surface_at(11, 3, 1):
            self.assertAlmostEqual(y, 1 + self.LY + 4.0 / 9.0, places=5)

if __name__ == "__main__":
    unittest.main(verbosity=2)