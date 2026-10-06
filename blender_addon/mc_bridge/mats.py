# -*- coding: utf-8 -*-
"""材质与贴图：占位色材质 + 后台拉取 PNG 贴图 + 主线程补全节点树。

材质键 = (block_id, facegrp(top/side/bottom))；
节点连接: Image(Repeat, Closest) × Color Attribute("Col") --Multiply--> BaseColor
"""
import os
import queue
import tempfile
import threading

import bpy

from .core import blocks as B

CACHE_DIR = os.path.join(tempfile.gettempdir(), "mcbridge_tex")

# (block, facegrp) -> {"state": pending|ready|done|failed, "png": path}
_pending = {}
_lock = threading.Lock()
_task_q = queue.Queue()
_fetcher_ref = None          # 后台线程使用的 tex_fetcher（连接期固定）
_worker = None

# 发光（R6）：/api/blocks 的逐状态亮度表（含模组方块）+ 全局 Emission 倍率
_light_map = None
_emission_scale = 4.0
# 改进自发光：发光颜色先过 对比度(2.1) -> 饱和度(0.9)，强度系数再 ×0.175——
# 贴图高光部分主导发光，避免原版式整块均匀泛光的"脏"观感
_emission_improved = True

_IMPROVED_CONTRAST = 2.1
_IMPROVED_SATURATION = 0.9
_IMPROVED_STRENGTH = 0.175


def set_emission_improved(on):
    """改进自发光开关：变更时重建全部 MCB_ 材质（节点拓扑不同）。"""
    global _emission_improved
    on = bool(on)
    if on == _emission_improved:
        return
    _emission_improved = on
    _drop_mcb_materials()


def set_light_map(m):
    """连接时设置一次：/api/blocks 下发的 "lights"（状态名 -> 0..15）。"""
    global _light_map
    _light_map = m if isinstance(m, dict) and m else None


def set_emission_scale(v):
    """全局发光倍率热更新：就地改写已有材质的 Emission 强度（无需重建）。

    材质节点里的 Strength 按"当时的倍率"写入，这里按比例缩放；
    旧倍率为 0 的材质没建发光节点，只能整体重建。"""
    global _emission_scale
    try:
        v = max(0.0, float(v))
    except (TypeError, ValueError):
        return
    old = _emission_scale
    if abs(v - old) <= 1e-6:
        return
    _emission_scale = v
    if old <= 1e-6:
        _drop_mcb_materials()
        return
    ratio = v / old
    for mat in list(bpy.data.materials):
        if not mat.name.startswith("MCB_") or not mat.use_nodes:
            continue
        nt = getattr(mat, "node_tree", None)
        if nt is None:
            continue
        for n in nt.nodes:
            if getattr(n, "type", "") == "ShaderNodeEmission":
                try:
                    n.inputs["Strength"].default_value *= ratio
                except Exception:
                    pass


def _drop_mcb_materials():
    for m in list(bpy.data.materials):
        if m.name.startswith("MCB_"):
            try:
                bpy.data.materials.remove(m)
            except Exception:
                pass


def _glow_of(block):
    """方块状态的发光亮度（0..15）。"""
    try:
        return B.light_of(block, _light_map)
    except Exception:
        return 0


def set_tex_fetcher(fetcher):
    """连接时设置一次（后台线程用）。"""
    global _fetcher_ref
    _fetcher_ref = fetcher


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run_worker, daemon=True)
        _worker.start()


def _run_worker():
    while True:
        block, facegrp = _task_q.get()
        if block is None:               # 停止哨兵
            return
        fetcher = _fetcher_ref
        try:
            if fetcher is None:
                raise RuntimeError("no tex fetcher")
            png = fetcher(block, facegrp)
            os.makedirs(CACHE_DIR, exist_ok=True)
            path = os.path.join(CACHE_DIR, "%s__%s.png"
                                % (block.replace(":", "_"), facegrp))
            with open(path, "wb") as f:
                f.write(png)
            with _lock:
                ent = _pending.get((block, facegrp))
                if ent is not None:
                    ent["state"], ent["png"] = "ready", path
        except Exception:
            with _lock:
                ent = _pending.get((block, facegrp))
                if ent is not None:
                    ent["state"] = "failed"


def material_name(block, facegrp):
    return "MCB_%s__%s" % (block.replace(":", "_"), facegrp)


def mat_key(block, facegrp):
    return block, facegrp


def _avg_color(block):
    try:
        _, _, _, color = B.block_info(block)
        return color
    except KeyError:
        return (0.6, 0.6, 0.6)


def _transparent(block):
    try:
        gid, cls, tint, color = B.block_info(block)
    except KeyError:
        return False
    return cls in (B.LIQUID, B.CUTOUT, B.TRANSPARENT)


def get_material(block, facegrp, tex_fetcher=None, overlay=None):
    """主线程调用：返回材质，贴图来源优先级 资产包 > 服务端拉取。
    tex_fetcher(block, facegrp) -> PNG bytes（后台线程执行）。

    overlay = 叠加层贴图 id（如草方块侧面的 side_overlay）：此时材质在着色器里
    把两层"合并"成一张面 —— 基底 + 叠加层 x tint（tint 仍来自顶点色，因此仍随
    群系变化），不新增任何几何。"""
    global _fetcher_ref
    if tex_fetcher is not None:
        _fetcher_ref = tex_fetcher
    name = material_name(block, facegrp) + (
        "" if overlay is None else "_ov%d" % overlay)
    mat = bpy.data.materials.get(name)
    if mat is not None and _has_image_node(mat):
        return mat
    if mat is None:
        mat = bpy.data.materials.new(name)
        _setup_placeholder(mat, block)
    # 1. 本地资产包（烘焙贴图）
    tid = _pack_face_tex(block, facegrp)
    if tid is not None and overlay is not None:
        pack = _pack()
        if pack is not None:
            _wire_overlay(mat, _pack_image(pack, tid), _pack_image(pack, overlay),
                          transparent=_tex_has_alpha(pack, tid))
            return mat
    if tid is not None:
        _apply_pack_texture(mat, block, tid)
        return mat
    # 2. 服务端异步拉取
    if tex_fetcher is not None:
        with _lock:
            if (block, facegrp) not in _pending:
                _pending[(block, facegrp)] = {"state": "pending"}
                _task_q.put((block, facegrp))
        _ensure_worker()
    return mat


def get_material_for(desc, tex_fetcher=None):
    """材质描述符 -> 材质。
    ("block", 方块名, facegrp[, 叠加层贴图id[, glow]]) 或 ("tex", texId[, glow])。"""
    if desc[0] == "tex":
        glow = int(desc[2]) if len(desc) > 2 else 0
        return get_model_material(int(desc[1]), glow)
    overlay = desc[3] if len(desc) > 3 else None
    return get_material(desc[1], desc[2], tex_fetcher, overlay)


def _pack():
    from .core import assets
    return assets.current()


def _pack_face_tex(block, facegrp):
    pack = _pack()
    if pack is None:
        return None
    faces = pack.default_faces(block)   # 传方块状态全名：v3 包按状态解析
    if faces is None:
        return None
    tid = {"top": faces[0], "side": faces[1], "bottom": faces[2]}.get(facegrp)
    return int(tid) if tid is not None else None


def _pack_image(pack, tid):
    """从资产包 RGBA 创建/复用 Blender 图像（主线程）。"""
    import numpy as np
    name = "MCBT_pack_%d" % tid
    img = bpy.data.images.get(name)
    if img is not None:
        return img
    (w, h), rgba = pack.texture_rgba(tid)
    img = bpy.data.images.new(name, w, h, alpha=True)
    px = np.frombuffer(rgba, np.uint8).reshape(h, w, 4).astype(np.float32) / 255.0
    img.pixels.foreach_set(px[::-1].ravel())      # Blender 像素自下而上
    return img


def _tex_has_alpha(pack, tid):
    import numpy as np
    _, rgba = pack.texture_rgba(tid)
    a = np.frombuffer(rgba, np.uint8).reshape(-1, 4)[:, 3]
    return bool((a < 255).any())


def _apply_pack_texture(mat, block, tid):
    pack = _pack()
    img = _pack_image(pack, tid)
    _wire(mat, img, block, transparent=_transparent(block),
          glow=_glow_of(block))


def get_model_material(tex_id, glow=0):
    """烘焙模型面材质（按贴图 id 共享；glow>0 时为发光变体，R6）。"""
    name = "MCB_tex_%d" % tex_id + ("_g%d" % glow if glow > 0 else "")
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    pack = _pack()
    mat = bpy.data.materials.new(name)
    if pack is None:
        _setup_placeholder(mat, "minecraft:stone")
        return mat
    img = _pack_image(pack, tex_id)
    _wire(mat, img, None, transparent=_tex_has_alpha(pack, tex_id), glow=glow)
    return mat


def _has_image_node(mat):
    nt = getattr(mat, "node_tree", None)
    if nt is None:
        return False
    return any(getattr(n, "type", "") == "ShaderNodeTexImage"
               for n in getattr(nt, "nodes", []))


def _setup_placeholder(mat, block):
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    c = _avg_color(block)
    # 视口(Solid 着色)显示色：Blender 实体模式不读节点树
    mat.diffuse_color = (c[0], c[1], c[2], 1.0)
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (c[0], c[1], c[2], 1.0)
        bsdf.inputs["Roughness"].default_value = 1.0
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    if _transparent(block):
        mat.blend_method = 'BLEND'


def flush(max_assign=8):
    """主线程调用：把就绪贴图接入材质（构造节点树）。返回处理数。"""
    ready_items = []
    with _lock:
        for key, ent in list(_pending.items()):
            if ent.get("state") == "ready":
                ready_items.append((key, ent))
                ent["state"] = "done"
            if len(ready_items) >= max_assign:
                break
    for (block, facegrp), ent in ready_items:
        _apply_texture(block, facegrp, ent["png"])
    return len(ready_items)


def _apply_texture(block, facegrp, png_path):
    mat = bpy.data.materials.get(material_name(block, facegrp))
    if mat is None:
        return
    c = _avg_color(block)
    mat.diffuse_color = (c[0], c[1], c[2], 1.0)
    name = "MCBT_%s__%s" % (block.replace(":", "_"), facegrp)
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.load(png_path)
        img.name = name
    _wire(mat, img, block, transparent=_transparent(block),
          glow=_glow_of(block))


def _wire(mat, img, block, transparent=False, glow=0):
    """构造节点树: Image × ColorAttribute("Col") -> Principled。

    glow>0（发光方块，R6）时追加 Emission 并 Add 叠加：
        Surface = Principled + Emission(发光色, strength = 亮度/15 × 倍率[×0.175])
    Cycles 下自发光面可真实照亮场景（萤石/岩浆/火把等效原版点光源）。
    改进自发光（默认开）：发光色 = 正片叠底结果 -> 对比度(2.1) -> 饱和度(0.9)，
    只让贴图高光部分主导发光（原版是整个模型均匀放光，观感脏）。"""
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    for n in list(nodes):
        nodes.remove(n)
    out = nodes.new('ShaderNodeOutputMaterial')
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    tex = nodes.new('ShaderNodeTexImage')
    tex.image = img
    # Blender 4.x 移除了 Image.extension/interpolation，改在节点上设置
    for attr, val in (("extension", 'REPEAT'), ("interpolation", 'Closest')):
        try:
            setattr(tex, attr, val)
        except (TypeError, AttributeError):
            pass
    vcol = nodes.new('ShaderNodeVertexColor')
    vcol.layer_name = "Col"
    mix = nodes.new('ShaderNodeMixRGB')
    mix.blend_type = 'MULTIPLY'
    mix.inputs["Fac"].default_value = 1.0
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0   # Blender 4.x
    except TypeError:
        pass
    bsdf.inputs["Roughness"].default_value = 1.0
    links.new(tex.outputs["Color"], mix.inputs["Color1"])
    links.new(vcol.outputs["Color"], mix.inputs["Color2"])
    links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    surf = bsdf.outputs["BSDF"]
    if glow > 0:
        emi = nodes.new('ShaderNodeEmission')
        strength = glow / 15.0 * _emission_scale
        if _emission_improved:
            strength *= _IMPROVED_STRENGTH
        emi.inputs["Strength"].default_value = strength
        color_src = mix.outputs["Color"]
        if _emission_improved:
            bc = nodes.new('ShaderNodeBrightContrast')
            bc.inputs["Contrast"].default_value = _IMPROVED_CONTRAST
            links.new(color_src, bc.inputs["Color"])
            hsv = nodes.new('ShaderNodeHueSaturation')
            hsv.inputs["Saturation"].default_value = _IMPROVED_SATURATION
            links.new(bc.outputs["Color"], hsv.inputs["Color"])
            color_src = hsv.outputs["Color"]
        links.new(color_src, emi.inputs["Color"])
        add = nodes.new('ShaderNodeAddShader')
        links.new(surf, add.inputs[0])
        links.new(emi.outputs["Emission"], add.inputs[1])
        surf = add.outputs["Shader"]
    links.new(surf, out.inputs["Surface"])
    if transparent:
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
        if block is not None and B.block_info(block)[1] == B.LIQUID:
            mat.blend_method = 'BLEND'
        else:
            mat.blend_method = 'CLIP'
    else:
        mat.blend_method = 'OPAQUE'


def _wire_overlay(mat, img_base, img_ov, transparent=False):
    """叠加层合成（草方块侧面那类「基底 + 带 tintindex 的 overlay」两层合一）。

    单个面片、单张贴图集就够，靠着色器把两层合并（与原版语义一致）：

        final = [ base x (1 - ov.a) + ov.rgb x ov.a x tint ] x AO

    其中 tint/AO 来自顶点色（rgb = tint x AO、a = AO），因此 tint 仍然随群系变化，
    而基底（泥土）不会被染色。这样既不需要额外面片，也不存在共面 Z-Fighting。"""
    mat.use_nodes = True
    nt = mat.node_tree
    nodes, links = nt.nodes, nt.links
    for n in list(nodes):
        nodes.remove(n)
    out = nodes.new('ShaderNodeOutputMaterial')
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    tex_b = nodes.new('ShaderNodeTexImage')
    tex_b.image = img_base
    tex_o = nodes.new('ShaderNodeTexImage')
    tex_o.image = img_ov
    for t in (tex_b, tex_o):
        for attr, val in (("extension", 'REPEAT'), ("interpolation", 'Closest')):
            try:
                setattr(t, attr, val)
            except (TypeError, AttributeError):
                pass
    vcol = nodes.new('ShaderNodeVertexColor')
    vcol.layer_name = "Col"
    # ov.rgb x tint
    m_tint = nodes.new('ShaderNodeMixRGB')
    m_tint.blend_type = 'MULTIPLY'
    m_tint.inputs["Fac"].default_value = 1.0
    # base x (1-ov.a) + (ov.rgb x tint) x ov.a
    m_ov = nodes.new('ShaderNodeMixRGB')
    m_ov.blend_type = 'MIX'
    # x AO（顶点色 alpha）
    m_ao = nodes.new('ShaderNodeMixRGB')
    m_ao.blend_type = 'MULTIPLY'
    m_ao.inputs["Fac"].default_value = 1.0
    try:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    except TypeError:
        pass
    bsdf.inputs["Roughness"].default_value = 1.0
    links.new(tex_o.outputs["Color"], m_tint.inputs["Color1"])
    links.new(vcol.outputs["Color"], m_tint.inputs["Color2"])
    links.new(tex_b.outputs["Color"], m_ov.inputs["Color1"])
    links.new(m_tint.outputs["Color"], m_ov.inputs["Color2"])
    links.new(tex_o.outputs["Alpha"], m_ov.inputs["Fac"])
    links.new(m_ov.outputs["Color"], m_ao.inputs["Color1"])
    links.new(vcol.outputs["Alpha"], m_ao.inputs["Color2"])
    links.new(m_ao.outputs["Color"], bsdf.inputs["Base Color"])
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if transparent:
        links.new(tex_b.outputs["Alpha"], bsdf.inputs["Alpha"])
        mat.blend_method = 'CLIP'
    else:
        mat.blend_method = 'OPAQUE'


def pending_count():
    with _lock:
        return sum(1 for e in _pending.values() if e["state"] == "pending")


def reset():
    with _lock:
        _pending.clear()
