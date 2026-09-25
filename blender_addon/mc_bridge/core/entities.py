# -*- coding: utf-8 -*-
"""实体几何（R5）：存档实体 NBT + 资产包 -> 可叠加进区块对象的几何。

覆盖两类实体：

**画（painting）**：自 1.21 起画变体由数据驱动
（`data/<ns>/painting_variant/<id>.json` 的 width/height/asset_id），
烘焙进 MCBA v4；这里按实体 NBT 的 variant / facing / Pos 生成一个平面四边形。
定位约定（NBT 字段语义可查证，几何落位含一处标注假设）：
  - variant -> (宽 w, 高 h, 贴图) 来自资产包；
  - facing  -> 画面朝向：0=南(+Z) 1=西(-X) 2=北(-Z) 3=东(+X)（wiki 明确）；
  - Pos     -> 画面底边中点（MC 实体原点位于包围盒底面中心的通用约定），
               再沿 facing 外移 1/32 方块（原版画与墙之间留有缝）。
  未与原版逐块对拍：Pos 的水平参考点（底边中点 vs 角块）与 1/32 这一定值
  来自通用约定；1.21.4 及以前另有 TileX/TileY/TileZ、1.21.5+ 改为 block_pos，
  如需严格一致需在真实存档 + 游戏画面上核对（见 docs/roadmap.md R5）。

**盔甲架（armor_stand）**：原版形状是代码定义模型（非 JSON），由
tools/be_models/DumpEntityModels.java 从客户端 jar 的模型工厂导出为**分层**
模型（每部件 pivot / 默认旋转 / 本地顶点，1/16 方块单位），烘焙进 MCBA v5
的实体模型段；这里按实体 NBT 的 Pose 在**运行时**装配：
  - Pose.Head/Body/LeftArm/RightArm/LeftLeg/RightLeg = [x, y, z]（度数）
    -> 对应部件 pitch/yaw/roll（弧度）；躯干木杆（left/right_body_stick、
    shoulder_stick）跟随 Body；
  - 部件显隐（对照 ArmorStandEntityModel.setAngles 反汇编）：leftArm/rightArm
    .visible = ShowArms，躯干木杆 / shoulder_stick 反之；base_plate.visible =
    !NoBasePlate；hat 默认隐藏；Marker / Invisible 整架不渲染；
  - 全局变换：rotateY(180 - Rotation[0])（原版 setupTransforms）、
    Small -> 缩放 0.5，模型**底边对齐实体 Pos**（脚底 = 实体位置，MC 通用约定）。
  注：原版实体模型是 **y 向下** 的（头部在 -y 侧），渲染器用 scale(-1,-1,1)
  翻转；这里同样翻转（x 镜像对左右对称的盔甲架无视觉影响）。逐块落位仍属
  "近似"，对拍见 roadmap R5。
"""
import math

import numpy as np

from . import mesher

_PAINTING_IDS = ("minecraft:painting", "painting")
_ARMOR_STAND_ID = "minecraft:armor_stand"

# 朝向 -> (法向单位向量, 画面右侧单位向量)；
# 右侧 = up × forward（观察者站在画正面时的右手方向）
_FACING = {
    0: ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),      # 南
    1: ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),     # 西
    2: ((0.0, 0.0, -1.0), (-1.0, 0.0, 0.0)),    # 北
    3: ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0)),     # 东
}
_GAP = 1.0 / 32.0                # 画面与墙面的间距（原版画不贴墙）


def painting_quads(ent, pack):
    """画实体 -> [(verts (4,3) float32 世界方块坐标, uv (4,2), tex_id)]。

    非画实体、缺字段、变体不在资产包里时返回 None（跳过该实体）。"""
    if str(ent.get("id") or "") not in _PAINTING_IDS:
        return None
    info = pack.paintings.get(str(ent.get("variant") or "")) if pack else None
    if info is None:
        return None
    w, h, tex = info
    if not tex or w <= 0 or h <= 0:
        return None
    facing = _FACING.get(int(ent.get("facing", 0) or 0))
    pos = ent.get("Pos")
    if facing is None or not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return None
    fwd, right = facing
    px, py, pz = (float(pos[0]), float(pos[1]), float(pos[2]))
    # 画面中心（= 底边中点沿 facing 外移一格缝）
    cx = px + fwd[0] * _GAP
    cy = py
    cz = pz + fwd[2] * _GAP
    hw = w / 2.0
    verts = np.array([
        (cx - right[0] * hw, cy, cz - right[2] * hw),          # 左下
        (cx + right[0] * hw, cy, cz + right[2] * hw),          # 右下
        (cx + right[0] * hw, cy + h, cz + right[2] * hw),      # 右上
        (cx - right[0] * hw, cy + h, cz - right[2] * hw),      # 左上
    ], np.float32)
    uv = np.array([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)], np.float32)
    return [(verts, uv, int(tex))]


# ------------------------------------------------------------ 盔甲架 ----

def _pose_of(ent, name):
    """Pose.<name> -> (pitch, yaw, roll) 弧度；缺省 (0,0,0)。

    MC 的 Pose 各分量是度数（EulerAngle 构造），渲染时 × 0.017453292 转弧度。"""
    pose = ent.get("Pose")
    v = pose.get(name) if isinstance(pose, dict) else None
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return tuple(math.radians(float(x)) for x in v[:3])
        except (TypeError, ValueError):
            pass
    return (0.0, 0.0, 0.0)


def armor_stand_quads(ent, pack):
    """盔甲架实体 -> [(verts (4,3) float32 世界方块坐标, uv (4,2), tex_id)]。"""
    model = pack.entity_models.get("armor_stand") if pack else None
    if model is None:
        return None
    # Marker / Invisible 时原版不渲染（无装备可显示）
    if ent.get("Marker") or ent.get("Invisible"):
        return None
    tex = pack.tex_id(model.get("tex"))
    if tex is None:
        return None
    small = bool(ent.get("Small"))
    show_arms = bool(ent.get("ShowArms"))
    no_base_plate = bool(ent.get("NoBasePlate"))
    rot = ent.get("Rotation")
    yaw = float(rot[0]) if isinstance(rot, (list, tuple)) and len(rot) >= 1 else 0.0
    pos = ent.get("Pos")
    if not isinstance(pos, (list, tuple)) or len(pos) < 3:
        return None
    body = _pose_of(ent, "Body")
    part_rot = {
        "head": _pose_of(ent, "Head"),
        "body": body,
        "left_arm": _pose_of(ent, "LeftArm"),
        "right_arm": _pose_of(ent, "RightArm"),
        "left_leg": _pose_of(ent, "LeftLeg"),
        "right_leg": _pose_of(ent, "RightLeg"),
        # 躯干木杆 / 肩杆跟随 Body 角度（原版 setAngles 对它们应用 bodyRotation）
        "left_body_stick": body,
        "right_body_stick": body,
        "shoulder_stick": body,
    }
    hide = {"hat": True}                      # 原版 hat 部件默认隐藏
    if show_arms:
        hide.update({"left_body_stick": True, "right_body_stick": True,
                     "shoulder_stick": True})
    else:
        hide.update({"left_arm": True, "right_arm": True})
    if no_base_plate:
        hide["base_plate"] = True
    quads = _assemble_model(model, part_rot, hide)
    if not quads:
        return None
    px, py, pz = (float(pos[0]), float(pos[1]), float(pos[2]))
    # 原版实体模型是 **y 向下** 的（头部在 -y 侧），渲染器用 scale(-1,-1,1)
    # 翻转；这里用对角矩阵 F 复刻（x 镜像对左右对称的盔甲架无视觉影响）。
    # 翻转后：原 max y 变底部 -> 底边 min_y = -(装配后 max y)/16。
    max_y = max(v[1] for q in quads for v in q[0])
    min_y = -max_y / 16.0
    s = 0.5 if small else 1.0
    flip = np.diag([-1.0, -1.0, 1.0, 1.0])
    g = _m_t(px, py - min_y * s, pz) @ _m_ry(math.radians(180.0 - yaw)) @ _m_s(s) @ flip
    out = []
    for verts, uv in quads:
        ws = []
        for x, y, z in verts:
            w = g @ np.array((x / 16.0, y / 16.0, z / 16.0, 1.0))
            ws.append((w[0], w[1], w[2]))
        # flip 是镜像（det<0）：反转顶点/UV 顺序保持外向绕序
        out.append((np.array(ws[::-1], np.float32), uv[::-1].copy(), tex))
    return out


def _assemble_model(model, part_rot, hide):
    """分层实体模型 -> [(verts (4,3) 1/16 单位局部, uv (4,2) Blender 约定)]。

    变换链（与 ModelPart 渲染一致）：M_child = M_parent · T(pivot) · Rz(roll) ·
    Ry(yaw) · Rx(pitch)；Pose 角度覆盖默认旋转（未给 Pose 的部件用模型默认）。
    UV 按 Blender 约定翻转 v 轴（MC 纹理 v 向下）。"""
    out = []

    def walk(part, m, _path):
        name = part["name"]
        px, py, pz = (float(v) for v in part["pivot"])
        pitch, yaw, roll = (float(v) for v in part["rot"])
        if name in part_rot:
            pitch, yaw, roll = part_rot[name]
        m2 = m @ _m_t(px, py, pz) @ _m_rz(roll) @ _m_ry(yaw) @ _m_rx(pitch)
        if not hide.get(name):
            for cub in part["cuboids"]:
                for face in cub["faces"]:
                    vs = []
                    for x, y, z, _u, _v in face["v"]:
                        w = m2 @ np.array((float(x), float(y), float(z), 1.0))
                        vs.append((float(w[0]), float(w[1]), float(w[2])))
                    uv = np.array([(float(u), 1.0 - float(v))
                                   for _x, _y, _z, u, v in face["v"]], np.float32)
                    out.append((np.array(vs, np.float32), uv))
        for c in part.get("children") or []:
            walk(c, m2, _path)
    walk(model["parts"][0], np.eye(4), "")
    return out


def _m_t(x, y, z):
    m = np.eye(4)
    m[0, 3], m[1, 3], m[2, 3] = x, y, z
    return m


def _m_s(s):
    m = np.eye(4)
    m[0, 0] = m[1, 1] = m[2, 2] = s
    return m


def _m_rx(a):
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    return m


def _m_ry(a):
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    return m


def _m_rz(a):
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


def build_geo(ents, pack, cx, cz, y_bottom):
    """一组实体 NBT -> geo 片段；顶点转到 (cx, cz) 区块/组的局部坐标。

    y 相对 y_bottom（与区块网格一致），x/z 相对 16*cx / 16*cz。"""
    quads = []
    for ent in ents or ():
        eid = str(ent.get("id") or "")
        if eid in _PAINTING_IDS:
            quads.extend(painting_quads(ent, pack) or ())
        elif eid == _ARMOR_STAND_ID:
            quads.extend(armor_stand_quads(ent, pack) or ())
    if not quads:
        return None
    ox, oz = 16.0 * cx, 16.0 * cz
    for verts, _uv, _tex in quads:
        verts[:, 0] -= ox
        verts[:, 1] -= y_bottom
        verts[:, 2] -= oz
    return mesher.entity_geo(quads)
