# -*- coding: utf-8 -*-
"""最小 bpy 模拟层：仅实现 MC Bridge 插件用到的 API 子集，用于无 Blender 环境的
冒烟测试。数据全部存在内存。"""
import sys
import types


# ---------------------------------------------------------------- 属性描述 ----

class _Prop:
    def __init__(self, name, kw):
        self.name = name
        self.kw = kw
        self.default = kw.get("default")

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        return obj.__dict__.get("_v_" + self.name, self.default)

    def __set__(self, obj, value):
        obj.__dict__["_v_" + self.name] = value


def _mk_prop(kw):
    def deco(name=None):
        return _PropDescriptor(kw)
    return _PropDescriptor(kw)


class _PropDescriptor:
    """bpy.props.XxxProperty(...) 返回的占位；PropertyGroup 元类将其转 _Prop。"""
    def __init__(self, kw):
        self.kw = kw


class _PGMeta(type):
    """真实 Blender 的 PropertyGroup 走 __annotations__ 注册属性。"""

    def __new__(mcs, name, bases, ns):
        anns = ns.get("__annotations__", {})
        for k, v in list(anns.items()):
            if isinstance(v, _PropDescriptor):
                prop = _Prop(k, v.kw)
                anns[k] = prop
                ns[k] = prop      # 注解语法不建类属性，这里显式注入
        for k, v in list(ns.items()):
            if isinstance(v, _PropDescriptor):
                ns[k] = _Prop(k, v.kw)
        return super().__new__(mcs, name, bases, ns)


# ---------------------------------------------------------------- 数据块 ----

class _VertexSeq:
    def __init__(self):
        self.co = []

    def add(self, n):
        self.co = [0.0] * (n * 3)

    def foreach_set(self, attr, seq):
        setattr(self, attr, list(seq))


class _LoopSeq:
    def __init__(self):
        self.uv = []
        self.vertex_index = []

    def add(self, n):
        self.vertex_index = [0] * n

    def foreach_set(self, attr, seq):
        setattr(self, attr, list(seq))


class _PolySeq:
    def __init__(self):
        self.loop_start = []
        self.loop_total = []
        self.material_index = []
        self.use_smooth = []

    def add(self, n):
        self.loop_start = [0] * n
        self.loop_total = [0] * n
        self.material_index = [0] * n
        self.use_smooth = [False] * n

    def foreach_set(self, attr, seq):
        assert hasattr(self, attr), attr
        setattr(self, attr, list(seq))


class _UVData:
    def __init__(self):
        self.uv = []

    def foreach_set(self, attr, seq):
        assert attr == "uv"
        self.uv = list(seq)


class _UVLayer:
    def __init__(self, name="UVMap"):
        self.name = name
        self.uv = []
        self.data = _UVData()          # Blender 4.x: foreach_set 在 .data 上

    def foreach_set(self, attr, seq):
        assert attr == "uv"
        self.uv = list(seq)


class _ColorAttrData:
    def __init__(self, n):
        self.color = []

    def foreach_set(self, attr, seq):
        assert attr == "color"
        self.color = list(seq)


class _ColorAttr:
    def __init__(self, name, type_="BYTE_COLOR", domain="CORNER"):
        self.name = name
        self.type = type_
        self.domain = domain
        self.data = _ColorAttrData(0)


class Mesh:
    _id = 0

    def __init__(self, name, users=0):
        Mesh._id += 1
        self.name = name
        self.users = users
        self.vertices = _VertexSeq()
        self.loops = _LoopSeq()
        self.polygons = _PolySeq()
        self.uv_layers = _Collection(_UVLayer)
        self.color_attributes = _Collection(_ColorAttr)
        self.materials = []
        self._validated = False

    def validate(self):
        self._validated = True
        return True

    def update(self):
        pass

    def from_pydata(self, verts, edges, faces):
        self.vertices.add(len(verts))
        self.polygons.add(len(faces))
        self.loops.add(sum(len(f) for f in faces))


class _Vec3:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class Object:
    def __init__(self, name, data=None, users=0):
        self.name = name
        self.data = data
        self.users = users
        self.parent = None
        if data is not None:
            data.users = data.users + 1
        self.location = (0.0, 0.0, 0.0)
        self.display_type = 'TEXTURED'
        self._matrix_world = types.SimpleNamespace(translation=_Vec3(0, 0, 0))

    @property
    def matrix_world(self):
        return self._matrix_world

    def __setitem__(self, k, v):
        self.__dict__["_" + k] = v

    def __getitem__(self, k):
        return self.__dict__.get("_" + k)

    def keyframe_point_insert(self, *a):
        return True


class _NodeSocket:
    def __init__(self, name):
        self.name = name
        self.default_value = None


class _LinkList:
    def __init__(self):
        self._links = []

    def new(self, a, b):
        lnk = (a, b)
        self._links.append(lnk)
        return lnk

    def __iter__(self):
        return iter(list(self._links))

    def __len__(self):
        return len(self._links)


class _Node:
    def __init__(self, ntype):
        self.type = ntype
        self.inputs = {}
        self.outputs = {}
        self.image = None
        self.layer_name = ""
        self.blend_type = None

    def __setitem__(self, k, v):      # tex.image = img etc.
        setattr(self, k, v)


class _NodeTree:
    def __init__(self):
        self.nodes = _Collection(lambda ntype: self._mk_node(ntype))
        self.links = _LinkList()

    def _mk_node(self, ntype):
        n = _Node(ntype)
        if ntype == 'ShaderNodeBsdfPrincipled':
            for s in ("Base Color", "Roughness", "Alpha", "Specular IOR Level"):
                n.inputs[s] = _NodeSocket(s)
            n.outputs["BSDF"] = _NodeSocket("BSDF")
        elif ntype == 'ShaderNodeOutputMaterial':
            n.inputs["Surface"] = _NodeSocket("Surface")
        elif ntype == 'ShaderNodeTexImage':
            n.outputs["Color"] = _NodeSocket("Color")
            n.outputs["Alpha"] = _NodeSocket("Alpha")
        elif ntype == 'ShaderNodeVertexColor':
            n.outputs["Color"] = _NodeSocket("Color")
        elif ntype == 'ShaderNodeMixRGB':
            n.inputs["Fac"] = _NodeSocket("Fac")
            n.inputs["Color1"] = _NodeSocket("Color1")
            n.inputs["Color2"] = _NodeSocket("Color2")
            n.outputs["Color"] = _NodeSocket("Color")
        return n


class Material:
    def __init__(self, name, users=0):
        self.name = name
        self.users = users
        self.use_nodes = False
        self.blend_method = 'OPAQUE'
        self.node_tree = _NodeTree()


class Image:
    def __init__(self, name, filepath="", users=0):
        self.name = name
        self.filepath = filepath
        self.users = users
        self.extension = 'REPEAT'
        self.interpolation = 'CLOSEST'


class Collection:
    def __init__(self, name, users=1):
        self.name = name
        self.users = users
        self.children = _ChildLinker()
        self.objects = _ObjectLinker()


class _ObjectLinker:
    def __init__(self):
        self._objects = []

    def link(self, obj):
        if obj not in self._objects:
            self._objects.append(obj)

    def unlink(self, obj):
        if obj in self._objects:
            self._objects.remove(obj)

    def __iter__(self):
        return iter(list(self._objects))

    def __contains__(self, o):
        return o in self._objects


class _ChildLinker:
    def __init__(self):
        self._children = []

    def link(self, coll):
        if coll not in self._children:
            self._children.append(coll)

    def unlink(self, coll):
        if coll in self._children:
            self._children.remove(coll)

    def __iter__(self):
        return iter(self._children)

    def __contains__(self, c):
        return c in self._children


class _Collection:
    """通用数据集合：new/get/remove/迭代。"""

    def __init__(self, new=None):
        self._items = []
        self._new = new

    def new(self, *args, **kw):
        item = self._new(*args, **kw)
        self._items.append(item)
        return item

    def get(self, name):
        for it in self._items:
            if it.name == name:
                return it
        return None

    def remove(self, item, **kw):
        if item in self._items:
            self._items.remove(item)
            data = getattr(item, "data", None)
            if data is not None:
                data.users = max(0, data.users - 1)

    def load(self, path):
        name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        name = name.rsplit(".", 1)[0]
        img = Image(name, path)
        self._items.append(img)
        return img

    def append(self, item):
        if item not in self._items:
            self._items.append(item)

    def __iter__(self):
        return iter(list(self._items))

    def __len__(self):
        return len(self._items)

    def clear(self):
        self._items.clear()


# ---------------------------------------------------------------- 场景 ----

class Scene:
    def __init__(self):
        self.camera = None
        self.frame_current = 1
        self.frame_start = 1
        self.frame_end = 250
        self.frame_preview_start = 1
        self.frame_preview_end = 250
        self.collection = Collection("Scene Collection")

    def frame_set(self, f):
        self.frame_current = f


# ---------------------------------------------------------------- bpy ----

class _Context:
    def __init__(self):
        self.scene = Scene()
        self.window_manager = types.SimpleNamespace(fileselect_add=lambda *a: None,
                                                    invoke_props_dialog=lambda *a: None)
        self.selected_objects = []


class _Handlers:
    def __init__(self):
        self.frame_change_post = []


class _Timers:
    def __init__(self):
        self.registered = []

    def register(self, fn, **kw):
        self.registered.append(fn)

    def unregister(self, fn):
        if fn in self.registered:
            self.registered.remove(fn)
        else:
            raise ValueError("timer not registered")


_registered_classes = {}
_operators = {}


def _register_class(cls):
    _registered_classes[cls.__name__] = cls
    if getattr(cls, "bl_idname", None) and hasattr(cls, "poll"):
        pass
    if hasattr(cls, "bl_idname"):
        _operators[cls.bl_idname] = cls


def _unregister_class(cls):
    _registered_classes.pop(cls.__name__, None)
    _operators.pop(getattr(cls, "bl_idname", None), None)


def call_operator(op_id, context, **kw):
    """测试辅助：模拟调用操作符。"""
    cls = _operators.get(op_id)
    if cls is None:
        raise RuntimeError("operator not registered: %s" % op_id)
    # 实例化（Operator 无 __init__ 参数）
    inst = cls()
    for k, v in kw.items():
        setattr(inst, k, v)
    ret = inst.execute(context)
    return ret


def _lib_write(path, blocks):
    with open(path, "wb") as f:
        f.write(b"FAKE_BLEND_LIB\x00" + str(len(blocks)).encode())
    return path


def build_bpy():
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(
        PropertyGroup=_PGMeta("PropertyGroup", (object,), {}),
        Operator=type("Operator", (object,), {}),
        Panel=type("Panel", (object,), {}),
        Object=Object,
        Scene=Scene,
    )
    # PropertyGroup 需要元类生效
    class PropertyGroup(metaclass=_PGMeta):
        pass
    bpy.types.PropertyGroup = PropertyGroup
    class _OperatorBase:
        def report(self, level, msg):
            self.last_report = (level, msg)

    bpy.types.Operator = _PGMeta("Operator", (_OperatorBase,), {})
    bpy.types.Panel = type("Panel", (), {})

    bpy.props = types.SimpleNamespace(
        StringProperty=lambda **kw: _PropDescriptor(kw),
        IntProperty=lambda **kw: _PropDescriptor(kw),
        FloatProperty=lambda **kw: _PropDescriptor(kw),
        BoolProperty=lambda **kw: _PropDescriptor(kw),
        EnumProperty=lambda **kw: _PropDescriptor(kw),
        PointerProperty=lambda **kw: _PointerProp(kw),
    )
    bpy.utils = types.SimpleNamespace(register_class=_register_class,
                                      unregister_class=_unregister_class)
    data = types.SimpleNamespace(
        meshes=_Collection(Mesh),
        objects=_Collection(Object),
        materials=_Collection(Material),
        images=_Collection(Image),
        collections=_Collection(Collection),
    )
    data.libraries = types.SimpleNamespace(
        write=lambda path, blocks: _lib_write(path, blocks))
    bpy.data = data
    ctx = _Context()
    bpy.context = ctx
    bpy.app = types.SimpleNamespace(handlers=_Handlers(), timers=_Timers())

    return bpy


class _PointerProp:
    """PointerProperty(type=SomePropertyGroup) 的描述符实现。"""

    def __init__(self, kw):
        self.kw = kw

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        cache = obj.__dict__.get("_ptr_cache")
        if cache is None:
            cache = self.kw["type"]()
            obj.__dict__["_ptr_cache"] = cache
        return cache

    def __set__(self, obj, value):
        obj.__dict__["_ptr_cache"] = value


def install():
    """安装（或复用）bpy 模块。幂等：同一进程内多个测试文件共享同一实例，
    否则各自 install 会得到独立的 data 集合，导致跨文件引用错位。"""
    existing = sys.modules.get("bpy")
    if existing is not None and getattr(existing, "_mcb_fake", False):
        return existing
    bpy = build_bpy()
    bpy._mcb_fake = True
    sys.modules["bpy"] = bpy
    return bpy
