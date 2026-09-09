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


def get_material(block, facegrp, tex_fetcher=None):
    """主线程调用：返回材质，贴图来源优先级 资产包 > 服务端拉取。
    tex_fetcher(block, facegrp) -> PNG bytes（后台线程执行）。"""
    global _fetcher_ref
    if tex_fetcher is not None:
        _fetcher_ref = tex_fetcher
    name = material_name(block, facegrp)
    mat = bpy.data.materials.get(name)
    if mat is not None and _has_image_node(mat):
        return mat
    if mat is None:
        mat = bpy.data.materials.new(name)
        _setup_placeholder(mat, block)
    # 1. 本地资产包（烘焙贴图）
    tid = _pack_face_tex(block, facegrp)
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
    ("block", 方块名, facegrp) 或 ("tex", texId)。"""
    if desc[0] == "tex":
        return get_model_material(int(desc[1]))
    return get_material(desc[1], desc[2], tex_fetcher)


def _pack():
    from .core import assets
    return assets.current()


def _pack_face_tex(block, facegrp):
    pack = _pack()
    if pack is None:
        return None
    faces = pack.default_faces(B.base_name(block))
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
    _wire(mat, img, block, transparent=_transparent(block))


def get_model_material(tex_id):
    """烘焙模型面材质（按贴图 id 共享）。"""
    name = "MCB_tex_%d" % tex_id
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    pack = _pack()
    mat = bpy.data.materials.new(name)
    if pack is None:
        _setup_placeholder(mat, "minecraft:stone")
        return mat
    img = _pack_image(pack, tex_id)
    _wire(mat, img, None, transparent=_tex_has_alpha(pack, tex_id))
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
    _wire(mat, img, block, transparent=_transparent(block))


def _wire(mat, img, block, transparent=False):
    """构造节点树: Image × ColorAttribute("Col") -> Principled。"""
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
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if transparent:
        links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
        if block is not None and B.block_info(block)[1] == B.LIQUID:
            mat.blend_method = 'BLEND'
        else:
            mat.blend_method = 'CLIP'
    else:
        mat.blend_method = 'OPAQUE'


def pending_count():
    with _lock:
        return sum(1 for e in _pending.values() if e["state"] == "pending")


def reset():
    with _lock:
        _pending.clear()
