# 路线图（Roadmap）

> 按优先级排列；标注 \[P0/P1/P2] 表示核心程度，\[易/中/难] 为实现成本。
> 欢迎按条目认领贡献；条目编号供 issue 引用（如 `R3`）。

## R2 原版材质/模型烘焙（客户端资产提取）\[P0·中] ✅ 已完成（v1.2）

> 已实现：`tools/bake_assets.py` 从客户端 jar / 资源包产出单文件 `MCBA1`
> （贴图 + 烘焙变体 + 方块状态表 + 逐面染色掩码 + 兜底分类）；
> `core/assets.py` 加载，`mats.py` 贴图优先级接入，网格器对 class5 注入真实模型。
> 控制/存档模式通用；`use_models` 控制 LOD0 是否走本地网格。验收项均通过
> （真实 1.21.1 jar 烘焙 + Blender 4.5 渲染确认）。

**现状**：贴图靠服务端 `mcbridge/textures/` 手工放置或程序化色块；
楼梯/栅栏等 class5 方块以完整方块近似，观感穿帮。

**方案**：新增「资产烘焙」工具链，从 **MC 客户端 jar / 资源包** 一次性提取：

1. **贴图烘焙**：解析 `assets/minecraft/textures/block/*.png` +
   方块状态→模型→贴图映射（`blockstates/*.json` → `models/block/*.json`），
   按「方块 × 面组」输出 16×16 PNG 包（含染色图，如草/叶的灰度 + tint 色）；
2. **模型烘焙**：解析原版方块模型 JSON（elements/from/to/faces/uv/rotation），
   把 class5 方块（楼梯/台阶/围栏/门/玻璃板…）烘焙成四边形流（含真实 UV），
   按「方块状态 → 几何」缓存为二进制资产包；
3. **分发格式**：单文件资产包（zip 或自定义 `MCBA1` 二进制），
   插件按 palette 名查找；服务端模式由模组打包下发，存档模式本地加载；
4. **染色**：草/叶/水保留 tint 顶点色管线（现有 `Col` 节点不变）。

**验收**：楼梯显示真实阶梯形状；草侧贴图带泥土过渡；栅栏为十字栏杆。

## R3 模组材质/模型烘焙 \[P1·中] ✅ 基本完成（v1.2）

> 烘焙工具已支持任意命名空间：把 `mods/*.jar` 一并作为输入即可
> （`python tools/bake_assets.py client.jar mods/*.jar -o assets.mcba`）。
> 已验证 ultramarine 模组（518 方块 / 2549 模型 / 983 贴图）。
> 剩余：见下方**剩余未完成**清单（群系染色已独立为 R8；发光/法线并入 R6；
> 控制模式的状态级 class 待改模组）；分类不准或动态代码模型方块可用
> `--classes` 手工标注（class / use\_model / 贴图）修正。
>
> 修复记录（v1.2.1）：① 通配符输入由脚本自行展开（PowerShell/cmd 不展开 `*`，
> 原先 `mods/*.jar` 直接报错，模组资源根本没进包）；② 资产包升到 MCBA v2，
> 顶点改存 1/256 方块单位——模组模型大量使用 0.25/0.5 像素等亚像素坐标，
> 按 v1 的 1/16 取整会塌成零面积面（实测 ultramarine 约 2.5% 面消失、55% 顶点偏移）；
> ③ blockstate 的 x/y 旋转次序修正为**先 x 后 y**（等价 MC 的 R\_y∘R\_x，用 MC 1.21.1
> 本体逐组核对 16 种组合）：原先先 y 后 x，导致 chain\[axis=x]、end\_rod /
> lightning\_rod 的四个水平朝向、楼梯 half=top 的 x=180 组合整体偏 90°/镜像。
> ④ 实现 `uvlock`：按 MC `BakedQuadFactory.uvLock`（`M = T(0.5)·D(d)·R⁻¹·D(d')ᵀ·T(-0.5)`，
> 96 组实测矩阵核对）锁定 UV 矩形并改写面的 rotation 索引，修复倒置楼梯贴图整体转 180°。
> ⑤ 类原版流体几何：按 MC `FluidRenderer` 的列高度（流体 level/9，水源 8/9）与
> 四角加权平滑生成顶/侧/底面，不再把水/岩浆画成整方块。仅本地网格路径
> （存档模式 / `mode=raw`）支持——服务端 MCM1 顶点为整型块坐标，表示不了
> 水面高度，故 `mesh_payload(fluids=False)` 默认保持整方块以维持跨语言一致。
>
> 修复记录（v1.2.2 · 流体几何三处修正，均由"与原版对拍 + 实机观感"暴露）：
>
> ⑥ **四角规则漏了原版** **`render`** **的首个分支**——本列 `h >= 1.0`（上方仍是同种流体、
> 整列满格）时四角直接取 1.0，**不进入加权平均**。漏掉后瀑布柱与多层水体的池壁侧面
> 从整格收到 0.8333（每格一段锯齿 + 格间缝隙）。同时按原版补齐两处：邻居列必须与
> **当前渲染流体**同种才按流体高度计入（水/岩浆相邻时互不借用）；实心判定改用
> `isSolid()` 语义（按碰撞箱，树叶满格 → 视为墙，否则水边被反常拉低）。
>
> ⑦ **方块状态的** **`level`** **与流体 level 相反**——`FluidBlock.statesByLevel[i] =
> getFlowing(8-i)`、`FlowableFluid.getBlockStateLevel() = 8 - getLevel()`（均自 1.21.1 jar
> 反汇编核对）。原先按方块 `level/9` 原样取高，等于把整片水面坡度**反过来**：紧邻水源的
> `level=1` 画成 1/9（应为 7/9）、最远的 `level=7` 画成 7/9（应为 1/9），于是出现
> "水源处凹陷、远处反而隆起"的反直觉坡度。现按 `level=0/8 → 8/9、level=N(1..7) →
> (8-N)/9` 取高。
>
> ⑧ **资产包给液体注入的整方块模型未剔除**——`pack.use_model()` 按模型占空比判定、
> 与 class 无关，真实资产包里 water/lava 同样是 `use_model=True`；`mesh_payload(fluids=True)`
> 原先只删贪心液体四边形、漏删 model 条目，导致同一格里整方块与流体几何**重叠**。
> 现在 fluids=True 时一并按液体 id 过滤 models（`fluids=False` 行为不变）。
>
> 测试：新增原版算法对拍 `tests/test_codec_mesher.py::TestFluidVanillaConformance`
> （内置按 javap 反汇编还原的 FluidRenderer 参考实现）、存档端到端
> `test_save_mode.py::TestSaveModeFluidGeometry`、液体模型重叠回归
> `TestFluidModelOverlap`，以及 `test_level_is_inverted_vs_fluid_level`。
>
> 补充：⑨ 手工分类标注表 `--classes`（`mcbridge-classes.json`），支持 class /
> use\_model / 贴图三类覆盖；给出 `tex` 后**动态代码模型**（无 JSON 模型）也能
> 按其贴图与分类烘焙成方块，不再只是程序化色块；未生效条目在烘焙结束告警。
> ⑩ 烘焙结束输出"近似几何"方块清单（代理模型 / 整立方兜底），替代原先设计中
> 并不存在的"导出清单中标记"。⑪ MCBA 升 **v3**：class / 染色掩码 / use\_model /
> 默认面下沉到**变体级**（按 blockstate 规则解析），修复 slab 的 double 被当半砖
> 注入模型、snowy 草方块贴图错等状态失真；读取端兼容 v1/v2。⑫ 动画贴图改为读
> `.mcmeta` 判定帧高（显式 height > frames 数量 > 竖直方形条带），不再纯靠尺寸猜测。
> R2 的推广：对**模组方块**同样生效。
>
> 修复记录（v1.3.0 · 四项，均由「真实 1.21.1 存档 + 资产包」实机测试暴露，
> 判据一律取自客户端 jar 反汇编）：
>
> ⑬ **资产包方块级查询未回退基础名**——存档/服务端传来的是带属性的方块状态名
> （`minecraft:oak_stairs[facing=east,...]`），而 v2 包的键是基础名，
> `default_faces` / `classify` / `tint_mask` / `use_model` 用全名直查**必然落空**
> → 整块贴图退回程序化色块、非完整方块退回整立方近似（实测相机周围 99 个状态中
> 86 个无贴图、92 个不注入模型）。新增 `_block_lookup()`：全名未命中时回退基础名。
>
> ⑭ **`uvlock`** **的旋转方向错了、且漏改面的 rotation 索引**——正确式为
> `M = T(0.5)·D(d)·R⁻¹·D(d')ᵀ·T(-0.5)`（原先少了 `R` 求逆与绕方块中心的平移）；
> 并且原版 `BakedQuadFactory.uvLock` 还会**一并改写面的 rotation 索引**，该索引决定
> 「角点 i ↔ 矩形角 (i+rot/90)%4」的对应，漏掉它则 90°/270° 下 u/v 轴该互换而未互换。
> 表现为 y≠0 的楼梯/栅栏各连接面贴图整体转 90°。已用本体 96 组矩阵、288 组
> （矩形 + 新 rotation + 每角点 UV）、1920 组按顶点位置的角点映射逐一核对。
>
> ⑮ **床沿 −Z 整体偏 1 格**——原版 `BedBlockEntityRenderer.renderPart` 的 z 平移是
> `translate(0, 0.5625, isFoot ? -1.0 : 0)`，而**世界路径**（`getWorld() != null`）
> 每半张床各画在自己那一格、`isFoot` 恒为 `false`；`-1.0` 只出现在 `getWorld() == null`
> 的背包「同时画头尾两半」分支。原先把同一组 ops 同时套给 head 与 foot，使全世界
> 50 个床格各偏 1 格（MC −Z = Blender +Y）。
>
> ⑯ **零厚度模型的共面面片导致 Z-Fighting**——`block/cross.json` 等零厚度模型给同一片
> 「纸」正反两面各写一个 face（north + south），两者顶点集合相同、绕序相反；原版剔除
> 背面所以各画一次是对的，而 Blender 默认双面渲染，两者**共面重叠** → 交叉面片植物呈现
> 黑白斑点噪声。烘焙期新增 `_dedupe_coincident()`（同顶点集且贴图/染色一致才合并），
> 网格器程序化交叉面片与 Java 侧 `emitCrosses` 同步由 4 面改为 2 面。植物
> （short\_grass / poppy / dandelion / fern / oak\_sapling…）4 → 2 面，ultramarine 等
> 模组的零厚度贴片一并去重（材质数不变）。
>
> 测试：新增 `test_bed_stays_in_its_own_block`（4 朝向 × 头/尾 包围盒必须落在本格）、
> `test_bed_legs_at_outer_ends`、`test_uvlock_rect_and_rotation_match_minecraft`、
> `test_cross_plant_two_diagonal_faces`、`test_zero_thickness_model_dedupes_coincident_faces`；
> 一致性夹具按新行为重生成，Java ↔ Python 仍逐字节一致。

- 模组方块模型注册走同一套 vanilla 格式（blockstates/models JSON），
  烘焙工具扫 `mods/*.jar` 内的 `assets/<ns>/` 即可覆盖；
- 遮挡分类启发式：**精确整立方判定**（单元素 `0,0,0→16,16,16` 且 ≥6 面）
  → 再按 `water/lava`、`glass/ice/pane/...`、贴图 alpha 依次判
  LIQUID / TRANSPARENT / CUTOUT，其余 NONCUBE。
  **未采用**"模型占空比 < 0.85 判 NONCUBE"——该阈值会把 15/16 高的模型误判为
  整立方体，从而错误剔除邻面（宁可保守判 NONCUBE，多算面也不穿帮）；
- 手工标注：`--classes mcbridge-classes.json` 修正启发式判不准的方块，支持
  `class` / `use_model` / `tex`（贴图）。仅对能烘焙出几何的方块生效，未生效
  条目在烘焙结束时告警；共享分类表（`core/blocks.py`）内的原版方块优先；
- **状态粒度信息（MCBA v3）**：class / 染色掩码 / use\_model / 默认面从"方块级"
  下沉到"变体级"，按 blockstate 规则解析。修复 `oak_slab[type=double]` 被当成
  半砖注入模型、`snowy` 草方块贴图错等失真；块级信息保留为兜底，v1/v2 包行为不变；
- 动画贴图读 `.mcmeta` 判定单帧高度（显式 `height` > `frames` 数量 > 竖直方形
  条带），不再只靠"高度是宽度整数倍"的尺寸猜测；
- 烘焙结束输出**近似几何方块清单**（代理模型 / 整立方兜底），替代原先设计中
  并不存在的"导出清单中标记"——便于量化模组覆盖度。

**剩余未完成**：

- 群系染色（草/叶/水按 biome 上色）——已独立为 **R8**，需要新的数据管线；
- 控制模式（默认 `mode=mesh`）的**状态级 class**：Java `BlockClassifier` 仍按
  方块 id 缓存与覆盖（`oak_slab` 的所有状态都判 NONCUBE），需要改模组并用真实
  游戏环境验证；存档模式与本地网格路径已按状态解析；
- **非常规模型 loader**（`neoforge:obj` 的 OBJ 模型、`neoforge:composite` 的复合
  模型，如 Create 的粉碎轮/飞轮/数码管）——烘焙当前只认标准 JSON `elements`，
  这类方块退化为整立方兜底，已独立为 **R12**。

## R4 跨区块贪心合并 \[P1·中] ✅ 已完成（v1.3）

> 已实现：调度与装卸的最小单位改为**区块组**（`Params.group` = 边长 1/2/4，
> 默认 2×2，UI「区块组」可调），组内一次网格化产出**一个** Blender 对象。
>
> ① 本地网格路径把「组 + 外圈一格」共 (group+2)² 个 payload 拼成一个填充体积
> （`assemble_padded(payloads, group)`，尺寸 `16*group+2`），贪心矩形因此可
> **跨区块边界**合并，组外圈一格只参与剔除、不发射几何；
> ② 服务端网格路径（LOD2 等）逐区块取 MCM1，按 `16*dx` 平移顶点后用
> `merge_geos` 拼成一个对象、材质表按内容去重（对象数下降、面数不变）；
> ③ 组距离取「组内最近区块到锚点」——用最近区块而非组中心，锚点所在组必定是
> LOD0，也不会因组中心落在半径外而漏加载锚点旁的区块；
> ④ 版本由组内逐区块版本聚合成**元组**，任一区块变化即重载整组（用 `max`
> 之类的聚合会漏掉小版本变化）。
>
> 实测对象数（`r_load=4`，圆盘内 49 区块）：1×1 → 49 / 2×2 → 17 / 4×4 → 8。
> 低于理论 group²（4/16）：组按绝对区块坐标对齐，锚点附近必然存在"只用到
> 1\~2 个区块"的边界组；组越大单对象越大、LOD 分级越粗、单帧应用负担越集中，
> 故默认 2×2 而非 4×4。
>
> **更正原描述**：区块边界的"可见接缝面"其实并不存在——3×3 padding 一直按
> 邻居剔除边界面、只发射中心区块的面。本项的真实收益是**对象数下降**（draw
> call）加上边界处矩形合并（跨区块减面 <2%，与预判一致）。
>
> 测试：`test_scheduler.py::test_group_reduces_objects`（1/2/4 组对象数、
> 组键对齐、顶点覆盖整组范围）、`test_codec_mesher.py::TestChunkGroup`
> （32×32 平板合并成 6 面、组外圈剔除组边界侧面、`merge_geos` 平移与材质去重）。

## R5 实体与方块实体 \[P2·难] ✅ 已完成（v1.4.0 画定位定案）

> 方块实体原版模型已完成（v1.3）：箱子/床/告示牌/旗帜/潜影盒/头颅/装饰陶罐/
> 传送门框架/钟等直接使用从客户端 jar 提取的原版几何 + 原版贴图。
>
> **存档实体读取 + 画 + 盔甲架 + 控制模式接口已完成（v1.3）**：
>
> ① `AnvilWorld.entity_payload` / `SaveClient.entities` 读 `<维度>/entities/r.x.z.mca`
> （实体与方块分开存放，格式同区块区域文件，根 compound 的 `Entities` 列表），
> 带 LRU 缓存；无实体文件的区块返回空表；
> ② 资产包升 **v4**：烘焙 1.21+ 数据驱动的画变体表
> （`data/<ns>/painting_variant/<id>.json` 的 `width`/`height`/`asset_id`
> 与 `textures/painting/`），实测原版 1.21.1 **50 个变体**全部正确
> （尺寸集合与 wiki 一致：1×1 / 1×2 / 2×1 / 2×2 / 3×3 / 3×4 / 4×2 / 4×3 / 4×4，
> 贴图恒 16 像素/格）；
> ③ `core/entities.py` 按实体 NBT 生成平面四边形，调度器在 LOD0 用 `merge_geos`
> 并入该组的区块对象（材质走 `("tex", 贴图 id)`，与烘焙模型面共用材质管线）；
> ④ **顺带修一个真 bug**：画贴图是无 `.mcmeta` 的 16×32 竖条（1×2 的画），
> 被上一版新增的"高度是宽度整数倍即动画条带"启发式误裁成 16×16（丢掉下半幅）；
> 现对画贴图显式关闭该启发式（`_ensure_texture(..., strip=False)`），
> 并由合成资源包的 16×32 画贴图用例回归覆盖。
>
> **未完成 / 待办**：
>
> - 画的**逐块定位**——✅ **已实测定案（v1.3.1）**：四面墙多尺寸画实测发现
>   整幅画向上偏移 h/2（h = 画高，与宽度无关、控制/存档模式一致），据此定案
>   **MC 实体 Pos = 画面中心**（水平居中 + 垂直居中），初版误按"底边中点"
>   锚定。修复：底边 = Pos.y − h/2（`core/entities.py`），回归测试断言
>   各尺寸画的垂直中心必须落在 Pos.y。1.21.4 及以前画的位置写在
>   `TileX/TileY/TileZ`、1.21.5+ 改为 `block_pos`，本实现直接用实体 Pos，
>   不受两者差异影响。
>   对拍工具 `tools/paintings_report.py` 保留（原始 NBT 字段 + 几何计算输出），
>   后续新版本对拍仍可用。
>
> **盔甲架（v1.3）**：原版盔甲架形状是代码定义模型（非 JSON），
> `tools/be_models/DumpEntityModels.java` 从客户端 jar 的模型工厂导出**分层**
> 模型（每部件 pivot / 默认旋转 / 本地顶点），烘焙进 **MCBA v5** 实体模型段；
> `core/entities.py` 在**运行时**按实体 NBT 的 Pose 装配（变换链与 ModelPart
> 一致，Pose 覆盖默认旋转；部件显隐对照 `ArmorStandEntityModel.setAngles`
> 反汇编：ShowArms 切换手臂/木杆、NoBasePlate 隐藏底座、Marker/Invisible
> 不渲染、Small 缩放 0.5、全局 rotateY(180-yaw)；模型 y 向下 → 渲染器
> scale(-1,-1,1) 翻转）。含单元测试（合成分层模型：Pose 旋转/显隐/缩放/底边
> 对齐/UV 翻转）与存档端到端测试。
>
> **控制模式实体（v1.3）**：模组新增 `GET /api/entities?dim&cx&cz`（只提取
> 画 / 盔甲架两种，字段见 `docs/协议规范.md`），`/api/ping` 声明 `entities: true`
> 做能力协商（旧版模组未声明时插件不请求，避免每次网格化 404）；
> `net.py::ApiClient.entities` 与 server\_sim 已接，调度器据此在 LOD0 把实体
> 并入组对象。**待真实游戏环境验证**（本环境无法编译/运行 MC 模组，
> 需用户构建模组并加载资产包后确认控制模式出画/盔甲架）。
>
> 作物生长**不需要额外工作**：`wheat[age=N]` 等本就是普通 blockstate 变体，
> 由 v3 状态粒度 + 按状态注入烘焙模型覆盖，故"作物生长状态驱动的贴图变体"
> 一项据此结项。

## R6 渲染增强 \[P2·易] ✅ 已完成（v1.4.0）

- 发光方块（萤石/岩浆）输出自发光节点（Emission × 原版 light 值）。
  ✅ 已完成：材质节点追加 **Emission + Add Shader**——
  `Surface = Principled + Emission(贴图×tint, strength = 亮度/15 × 倍率)`，
  Cycles 下自发光面可**真实照亮场景**（萤石/岩浆/火把等效原版点光源）。
  亮度来源三级：① 控制 `/api/blocks` 新增 `lights` 逐状态亮度表
  （`state.getLuminance()`，含模组方块与状态相关亮度，如点亮的红石灯）；
  ② 插件内置原版亮度表兜底（存档模式 / 旧模组；状态相关的红石灯、蜡烛、
  海泡菜、重生锚、light 方块等按原版公式解析状态属性）；③ 未收录为 0。
  面板「自发光强度」倍率（默认 4.0，0 关闭）改动**即时生效**——就地缩放
  已有材质的 Emission Strength，无需刷新；烘焙模型路径（火把/灯笼等
  非整方块模型）按贴图聚合面亮度下发 glow（`("tex", id, glow)`）。
  **改进自发光**（默认开）：发光颜色 = 正片叠底结果 -> 对比度(2.1) ->
  饱和度(0.9) -> Emission.Color，强度系数自动 ×0.175（默认 4.0 → 0.7）——
  贴图高光部分主导发光，避免原版式整块均匀泛光的观感；开关变更时材质
  自动重建。**总开关**与**两种可叠加的筛选策略**（实机反馈补齐）：
  亮度属性策略（lights 表/内置表逐状态精确）与 ID 关键词策略
  （lamp/candle/lantern/torch/glow/fire/lava… 整词命中按满亮度 15 发光，
  兜底模组未声明 luminance 的"灯"类方块；存档模式补齐 16 色蜡烛/
  蜡烛蛋糕同公式）。
  剩余：无（存档模式模组方块可再由关键词策略覆盖）。

## R7 稳定性与生态 \[P1·易] ✅ 已完成

- `load_post` 残留对象处理（提示转静态或清除，当前仅有手动卸载）。
  ✅ 已完成：注册 `load_post` 处理器——按**文件路径变化**区分真实切档与
  memfile 撤销（undo/redo 也触发 load\_post，同路径只重建材质缓存不重置连接）；
  切档时作废旧连接、检测场景中的 `MCB_` 残留对象并在面板提示
  **转为静态保留 / 全部清除**（`mcb.leftover_keep` / `mcb.leftover_clear`）；
- 进度条 UI（预热/首载时显示区块 x/y）。
  ✅ 已完成：`step_tick` 按调度器计数（LIVE / QUEUED+FETCHING+READY）计算
  组级进度，面板显示"加载中/预热中 x/y (z%)"（Blender 4.x 用 `layout.progress`
  进度条控件，旧版回退文本），状态栏同步驱动原生 `wm.progress_*`；
- CI：GitHub Actions 跑 pytest + Java 对拍 + 打包发布（zip/jar artifact）。
  ✅ 已完成（v1.3.1）：`ci.yml`（push/PR → pytest 全量 + 夹具再生对拍 +
  `gradle build` 含跨语言一致性对拍 + `pack_release.py` 打包 artifact）、
  `release.yml`（打 `v*` tag 自动发版，内置 GITHUB\_TOKEN 免 PAT）。

## R8 群系染色（biome tint）\[P1·难]

**存档模式：✅ 已完成** · **控制模式：✅ 已完成（v1.3.1，MCC1 v2 携带 section 群系）**

**做法**（方案 B：烘焙真值表，不在插件里复刻算法）

- **配色**：烘焙期从客户端 jar 的 `data/minecraft/worldgen/biome/*.json` 与
  `assets/minecraft/textures/colormap/{grass,foliage}.png` 算出每个群系的
  `(grass, foliage, water)` RGB，写进 **MCBA v6 群系段**（64 项）；
- **取值公式与常量全部对齐原版字节码**，不靠推理：
  `GrassColors.getColor` 的 `k = (j << 8) | i` 索引（colormap png 按行主序）、
  `clamp(temperature, 0, 1)`（沙漠的 `temperature=2.0` 靠这个兜住）、
  `DARK_FOREST` 的 `((c & 0xFEFEFE) + 0x28340A) >> 1`、`SWAMP` 的 `6975545`；
  `water_color` 走 biome effects 本身，不走 colormap；
- **数据源**：`anvil._decode_biomes` 读 section 的 4×4×4 调色板（bits 下限 1，
  且兼容"只有 palette 没有 data"的单值形态——这是绝大多数 section 的形态），
  索引序与方块一致 `(y, z, x)`；
- **网格**：群系维度进入贪心合并键，**只对声明染色的方块生效**；顶点色改为按
  "面所属格子的群系"取色，覆盖完整方块面 / 烘焙模型面 / 流体几何三条路径；
- **兜底**：无群系数据（或群系不在表里）时行为与改前完全一致。

**验证**

- Python 复刻 vs 原版 Java **逐点对拍 0/64 不一致**（`tools/biome_colors/ProbeColormap.java`）；
- plains/forest/swamp/desert/snowy\_plains/jungle/taiga/dark\_forest **八色命中 wiki 真值**；
- 真实存档端到端：swamp `#6A7039`、desert `#BFB755`、plains `#91BD59` 三色精确命中，
  且与 `blocks.py` 原有的"平原常量"完全自洽（平原区颜色不变）；
- 几何代价：真实 fillbiome 布局 +0.05%；密集群系最坏 +20.8%，
  "只让染色方块进键"可压到 **+1.3\~1.6%**；
- 回归测试 `tests/test_biome_tint.py`（20 例）。

**剩余（控制模式）**：✅ 已随 v1.3.1 完成——MCM1 v2 携带逐面群系 id、
MCC1 v2 携带 section 群系（4×4×4 名表 + 下标），两条路径共用同一套客户端染色。

## R9 按摄像机距离优先加载 \[P1·易] ✅ 已完成

**可行性结论：✅ 技术可行，且可以做到吞吐零损耗**（排序对象是小队列，
不涉及额外 HTTP 或串行化）。

> 实现记录：① `tick()` 检测锚点跨组移动后重建 QUEUED 队列距离并重新
> heapify（旧入队序作废，O(n)）；② `poll_apply` 出队前按组距离排序 ready
> （网络抖动下远组先完成也不插队）；③ 面板「按摄像机距离优先」开关
> （默认开）；④ 派发保持并行（inflight 全填满），无串行化。单测 3 项：
> 乱序完成按距离应用 / 关闭保持完成序 / 锚点重排。

**现状**（大半已有，缺口在三处细节）：

- 派发队列**已经**按组距离（欧氏）优先：`_push(d, key)` 入堆、`_drain` 按距离
  出堆派发（`inflight` 个并行槽），失败重排队附加 +2 距离惩罚——近处区块
  本来就先拿并行槽；
- 缺口 ①：优先级在**入队时定格**，锚点移动后队列里还压着旧距离的排序；
- 缺口 ②：`poll_apply` 按**完成序**应用——网络抖动时远组可能后完成却先上屏；
- 缺口 ③：无用户开关（现行为是隐式的）。

**方案**：面板「按摄像机距离优先」开关（默认开）

1. **锚点重排**：锚点跨组移动时对 QUEUED 组重算距离并重入队
   （O(n log n)，n = 待载组数通常 < 200，<1ms；节流到不与 tick 冲突）；
2. **按距离应用**：`poll_apply` 返回前把 ready 队列按组距离排序
   （n ≤ ready 长度，微秒级）——近组先上屏，视觉上"从脚下往外长"；
3. **不串行化派发**：并行槽照常填满——若为严格就近而把 inflight 降为 1
   会产生队头阻塞（最远的慢请求拖住全部），**明确不做**，这正是"不损耗
   加载效率"的关键取舍；
4. 组间共享的邻域 payload 由 ChunkStore 在途去重，排序不影响请求总数。

**验收**：快速平移/跳跃镜头时近处区块先于远处出现；吞吐（组/秒）与
关闭时无差异；单测模拟乱序完成（远组先 ready）断言应用序列按距离。

## R10 区块更新策略（开关 / 事件驱动 / 手动 / 白黑名单）\[P1·中] ✅ 已完成

**可行性结论：✅ 全部可行**——总开关、手动更新、白黑名单为纯插件侧；
事件驱动需要模组小改（全局修订号），并提供回退。

> 实现记录：① 「自动更新」总开关（两模式）；② **事件驱动**：模组在
> `VersionTracker.bump`（WorldChunkMixin setBlockState 钩子）同步递增
> **全局世界修订号**，新增 `GET /api/worldrev`——插件以 **event\_interval**
> （默认 0.5s；仅取一个整数，开销≈0）轮询，变更才做逐组版本扫描，
> 方块改动亚秒级生效；旧模组 404/缺方法自动永久回退常规轮询
> （version\_interval，默认 5s）；
> ③ 存档模式自动更新：`SaveClient.world_stamp()` 变化 → 先 `invalidate()`
> （清 payload/实体/网格缓存并重开 region 句柄，防偏移表过期）→ 重扫受管
> LIVE 组；④ 白/黑名单（组级，`mcb.list_manage` 操作符 + 面板「区块管理」
> 框，随 .blend 保存）——黑名单优先、白名单无视总开关；⑤ 「立即刷新」
> 修复存档模式缓存失效。单测 5 项：修订号门控 / 旧模组回退 / 名单语义 /
> 存档戳重载 / 操作符同步。
>
> **③的实机修正（后续补）**：原实现的变更戳是"**全部维度所有 .mca 的
> mtime+size 之和**"标量——游戏与 Blender 同时开着同一存档时，MC 周期性
> 写盘使戳几秒一变，触发**全视野**重载（实机反馈"存档模式每隔几秒自动
> 刷新"）。改为 `world_stamp(dim)` 返回**该维度** region/entities 逐文件的
> 指纹集合（frozenset），调度器对比新旧集合得到**变化的 region 文件**
> （32×32 区块），只重载覆盖其中区块的组、只失效这些区块的 payload 缓存，
> `invalidate(dim)` 也收敛到单维度——其他维度的缓存与句柄保留。单测同步
> 加"未受影响 region 的组不动 / 其它维度写盘不触发"两条断言。

**现状**：

- 控制模式：`version_interval`（默认 5s）逐组轮询版本，变更重载；无开关；
- 存档模式：轮询恒关（静态快照）——用户改存档后无感知；
- 手动更新：「立即刷新」已有，但**存档模式下不清 payload/网格缓存**，
  刷新拿到的是旧数据（真 bug，随本项修复）。

**方案**：

1. **自动更新总开关**（两模式）：关闭后 LIVE 组永不自动重载。
   存档模式开启时按 **region 文件 mtime/版本号**低频比对（不重新全量解析），
   变化才重扫对应组——成本可控；
2. **事件驱动（控制模式可选策略）**：模组新增**全局世界修订号**
   （任一方块更新 +1；snapshot 服务钩子或 ServerWorld mixin），
   插件只轮询这个整数（单请求、开销≈0），**变更才**做逐组版本比对——
   把"固定 5s 全量轮询"变成"无改动零成本、有改动即时感知"；
   旧模组无修订号字段 → 自动回退现有轮询（能力协商沿用 `/api/ping`）；
3. **手动更新（两模式）**：修正存档模式刷新——先失效 payload/网格缓存
   再重排队；控制模式维持版本号失效路径；刷新操作符支持"仅刷新选中对象"；
4. **白名单 / 黑名单**（组级，与 R11 共用选择交互）：
   - 添加方式：视口选中 MCB\_ 对象 → 操作符入列（组键 dim,gx,gz），
     面板列表可查看/移除（CollectionProperty，随 .blend 保存）；
   - **白名单** = 强制更新：总开关关闭也更新（可配独立更高轮询频率）——
     适用"边拍边改"的舞台区；
   - **黑名单** = 永不自动更新——适用大背景区（掉落沙/水流/其他玩家改动
     引发的无意义重载）；
   - 冲突：黑名单优先；名单组仍受 R11 卸载规则约束（两名单语义独立）。

**验收**：单测覆盖修订号触发/名单过滤/黑名单优先；实机：事件策略下
游戏内改方块数秒内更新、无改动时无逐组轮询请求（日志可证）；
黑名单组改动不触发重载；存档模式手动刷新可见改动。

## R11 常见区块（钉选，不被主动卸载）\[P2·易] ✅ 已完成

**可行性结论：✅ 纯插件侧，低难度**（调度器已有 frozen 全局冻结原语，
本项是其组级、持久化版本）。

> 实现记录：① 调度器 `pinned` 组键集合——卸载判定跳过；②
> `ensure_pinned()` 幂等补载（无距离限制）供「自动预载」（连接后、
> 「卸载全部」后每 tick 调用）；③ 名单序列化存场景属性
> （`dim:gx,gz;...`，随 .blend 保存），step\_tick 按 (调度器id, 字符串)
> 缓存同步；④ `mcb.list_manage` 操作符（名单+操作双枚举，R10 白黑名单
> 复用）+ 面板「区块管理」框；⑤ 「卸载全部」同时清调度器状态（在途
> 抓取安全丢弃），范围内组重入队、钉选组自动拉回；⑥ 超 64 组告警。
> 单测 4 项。

**现状**：卸载只由迟滞半径决定（超出 `r_unload` 即删）；prewarm 的冻结是
全局且临时的。

**方案**：

1. **设为常见区块**：视口选中 MCB\_ 对象（或输入坐标）→ 操作符钉选，
   面板列表管理（与 R10 白黑名单共用交互组件）；
2. **调度器**：卸载判定跳过钉选组（`if key in pinned: continue`）——
   移出 `r_unload` 后对象仍在场景、仍可被相机回看；
3. **自动预载**（默认开）：连接后无论距离自动加载全部钉选组
   （等效一次性预热）——"常见区块"的价值就在重连/开档即得；
4. **边界约定**：钉选只防卸载，内容更新仍按 R10 策略（可配白名单配合）；
   「卸载全部」作为显式动作允许清掉钉选对象（钉选记录保留，
   自动预载开启时下个 tick 拉回）；钉选数量超阈值（如 64 组）告警内存；
5. 存储：场景属性随 .blend 保存（渲染农场复现布局）。

**验收**：钉选组移出卸载半径后对象仍在、回看零等待；未钉选照常卸载；
重连后钉选组自动预载；「卸载全部」+ 自动预载的交互符合上述约定；
单测覆盖钉选跳过卸载 / 预载 / 数量告警。

**面板组织**：R10/R11 的名单管理共用一个「区块管理」子面板
（常见区块 / 更新白名单 / 更新黑名单三列表 + 添加/移除操作符），
开关与策略选项进「加载策略」框。

## R12 非常规方块模型 loader 支持（OBJ / composite）\[P1·中] ✅ 已完成

> **实现记录（`tools/bake_assets.py`，两侧加载器共用）**：
>
> ① **`neoforge:composite`** **展开**（`ModelLoader._composite_elements`）：children
> 各是完整子模型，展开成一份 elements 列表。关键点是**把每个 child 的面贴图引用
> 就地解析成具体贴图名再合并**——各 child 普遍都用 `#0`/`#1`，不固化就会在合并后
> 的公用纹理槽里互相覆盖。
>
> ② **`neoforge:obj`** **解析**（`ModelLoader._parse_obj` / `_parse_mtl`）：
> 解析 `.obj`（`v`/`vt`/`vn`/`f`/`usemtl`/`mtllib`）与配套 `.mtl`。Create 的
> MTL 写的正是 `map_Kd #3` 这类**纹理变量引用**（与 MC 模型同源），故贴图解析
> 直接复用 `resolve_texture`；OBJ 常被多个 blockstate 复用换色（16 色阀门把手
> 共用一份 `.obj`，靠 `#3` 取色），所以**贴图解析必须发生在合并后的纹理槽上**。
> UV 语义对齐 NeoForge `ObjModel`：`v_mc = flip_v ? 1 - v_obj : v_obj`，再按
> Blender 约定翻转；顶点 ×16 转 1/16 单位（OBJ 用 0\~1 方块单位）；面方向取 vn
> 的**主导轴**。
>
> ③ **`#`** **前缀容错**：`resolve_texture` 现兼容无 `#` 的变量引用
> （1.21 原版 `heavy_core` 写 `"all"`），修掉一个原版方块的几何丢失。
>
> ④ 只接受三角/四边形面（Blender 导出即四边形）；多边形计入 `_STATS["obj_poly"]`。
>
> **产物对比（NeoForge 实例：原版 + Create 6.0.10 + ultramarine）**：
>
> | 指标          | 实施前          | 实施后                      |
> | ----------- | ------------ | ------------------------ |
> | 近似几何（兜底）方块  | 56           | **14**                   |
> | create 兜底方块 | 45           | **4**（2 流体 + 2 Flywheel） |
> | 方块数 / 变体数   | 2254 / 21354 | 2254 / 21354（不变）         |
> | 贴图数         | 3327         | 3331                     |
>
> 41 个 create 方块全部出真实几何（`crushing_wheel` 268 面/变体、
> `large_water_wheel` 1738、`track` 51、阀门把手 60、数码管 22、`display_link` 33、
> `stock_link` 34…），顶点单位与 elements 分支**完全同口径**（1/16 网格，
> 支持亚像素与越界，如粉碎轮 y 到 22.8）。剩余 14 个兜底项全部无害：
> 原版 9 个（water/lava/barrier/light/end\_portal 等本无方块几何）+
> create 4 个（chocolate/honey 是流体；belt/factory\_gauge 见方向 1 结论）+
> ultramarine 1 个。
>
> **未做**：其余 NeoForge 内置 loader（`separate_transforms` 服务物品视角、
> `empty` 无几何、`elements` 语义近似标准）无方块收益，故不实现；Fabric 侧无
> 内置 loader（见下），本轮不含。

**方向 3 落地（后续补全，`_BE_PARTS`** **表）**：实机复验发现"部件缺失"的真正
大头不是 blockstate 方块，而是 **BE/Flywheel 渲染器动态绘制的部件**（风扇叶、
钻头、搅拌头、压板、表盘、大炮炮管、整条传送带）。排查（`dist/_r12_be_models.py`：
扫"不被任何 blockstate 引用的模型"）发现这些部件在 jar 里都带了**静态 JSON
模型**，只是只有渲染器实例化它们——于是按方向 3 的思路落地，但用**真实模型**
替代原设想的盒子模板（`tools/bake_assets.py`）：

- `_BE_PARTS` 表：方块 -> 部件规格（`model` / `when`（按变体属性选模型，
  首中即停）/ `pre_rot`（部件制作姿态 -> block 模型姿态对齐）/ `copy_rot`
  （BE 多实例部件复制 N 份））；`_variant` 在 blockstate 变换前叠加部件几何，
  `_block_info` 块级信息吃无条件规格；
- 传送带按 `part/slope` 选 `belt/{start,middle,end}`（平面，`vertical` 由
  blockstate `x=90` 竖起、`sideways` 由 `x=180` 翻面）或 `belt/diagonal_*`
  （自带 45° 斜角）；`part=pulley` 额外叠 `belt_pulley`（木质皮带轮）；
  风扇叶 `pre_rot x=+90`（对齐"变速箱在下"的 block 姿态）+ 绕 Y 复制 4 片；
  钻头 `x=-90`（锥朝 +Z -> 朝上）；搅拌头 + 吊杆 `pole`（y 7..32 出界直接
  叠加）；压板自带活塞杆（y 到 27）直接叠加；
- 朝向与分段经**软件渲染预览逐块目检**（`dist/_r12_preview_parts.py`）+
  叶片位置数值验证（每朝向紧贴前格栅内侧）；
- 姿态强依赖运行时的不做：机械臂关节（部件坐标按 BE pivot 摆放，模型 JSON
  无法恢复）、部署器手、绳索/软管长度、运动中的活塞/大炮等装置。

| 指标                       | 方向 2 后     | 方向 3 后          |
| ------------------------ | ---------- | --------------- |
| belt 变体几何                | 6 面（整立方代理） | 7\~56 面（分段真实形状） |
| encased\_fan             | 14 面（仅外壳）  | 42 面（+4 叶片）     |
| mechanical\_drill        | 22 面       | 80 面（+钻头）       |
| mechanical\_mixer        | 20 面       | 27 面（+搅拌头）      |
| mechanical\_press        | —          | 48 面（+压板/活塞杆）   |
| speedometer/stressometer | 12 面       | 25 面（+表盘）       |
| schematicannon           | 18 面       | 58 面（+炮管/支架）    |

变体数 21354 -> 21394（+40，belt 分段组合），包体 43.0 -> 43.2 MB。

**第三轮补全（实机复验，`shaft_half`** **与锁链传动轮）**：用户反馈"大部分方块仍缺部件"
后重新普查（`dist/_r14_parts_scan.py`：列出「有模型但 blockstate 从不引用」的孤儿部件，
54 个方块命中），并用**字节码取证**确定归属（`dist/_r14_java*.py`：从 Create jar 的
`.class` 常量池读出渲染器类引用的部件字段名）：

- **`SHAFT_HALF`**（`create:block/shaft_half`，沿 +Z 制作的半轴）被
  `GearboxRenderer/GearboxVisual`、`SplitShaftRenderer/SplitShaftVisual`、
  `EncasedCogRenderer/EncasedCogVisual`、`BearingRenderer`、`CreativeMotorRenderer`、
  `EncasedFanRenderer/FanVisual`、`SawRenderer/SawVisual`、`CuckooClockRenderer`
  引用 → 对应 `gearshift`（反转齿轮箱）/`clutch`（离合器）/`gearbox`（十字齿轮箱）/
  `*_encased_cogwheel` 等，中间那根轴此前完全没进包；
- 朝向：`shaft_half` 沿 +Z，`pre_rot` 绕 X 轴 ∓90°（原点取方块中心）得 ∓Y 半轴；
  gearshift/clutch 的制作姿态正是 axis=Y（blockstate `axis=y` 无旋转），且其模型
  顶/底面用的是**半透明** `andesite_funnel_frame`——透过它能看到轴，缺轴时就是
  用户截图里那个"黑洞"；齿轮箱顶/底是实心 `andesite_casing`，故其轴取**水平** +Z
  （`GearboxVisual` 以 SOUTH 为制作默认，其余朝向运行时轮询供电方向）；
  安山/黄铜齿轮箱的轴由 `top_shaft`/`bottom_shaft` 状态控制，按 `when` 分别叠加；
- ~~`create:chain_conveyor`：叠加轮/轴/护罩~~ **已回退**——`ChainConveyorVisual`
  字节码里有 `setupGuards` / `deleteGuards` / `guards`，说明它的外壳与护罩是
  **按连通方向在运行时取舍**的（静态 blockstate 无此信息），直接把三份 OBJ 叠加
  会得到一个"状态不对"的外壳（实机反馈）。此项回到未处理；
- **第四轮修正（传送带 / 齿轮 / 机壳轴，实机第 3 次反馈后）**：
  ① **传送带**把平面段拆成「上半圈」`belt/{start,middle,end}` 与「下半圈」
  `belt/*_bottom`（回程）两支，斜段 `belt/diagonal_*` 自带上下两圈故与平面段
  **互斥**（按 slope 选择）——之前只加了上半圈，整条带子是个"空心框"里只有一片，
  与游戏里完整的皮带环不符；`belt_pulley` 修正朝向：它按**竖直轴**制作，绕 Z 轴
  −90°（原点取中心）把轴转到 X，才垂直于带宽方向（水平传送带的轮轴本就该水平）；
  ② **齿轮**：`EncasedCogRenderer` 的 `SHAFTLESS_COGWHEEL` /
  `SHAFTLESS_LARGE_COGWHEEL` 对应 `create:block/{cogwheel,large_cogwheel}_shaftless`
  ——齿轮箱的**齿轮本体**此前完全没进包（上轮只补了轴），现按大/小齿轮箱分别叠加；
  ③ **机壳轴**（`andesite_encased_shaft` / `brass_encased_shaft`）：静态模型只有
  外壳（5 元素、无 4×4 轴截面），补 ±Y 半轴。
  注：`create:cogwheel` / `large_cogwheel` 的静态模型**自带** `[6,0,6]→[10,16,10]`
  整轴（7 / 16 元素），无需补——它们的 `COGWHEEL_SHAFT` 只在"带支架"变体用。
- 验证：软件渲染预览改为**按不透明像素取代表色 + alpha 合成**（旧版用整图平均色，
  会把半透明贴图渲染成黑块、掩盖真实情况），逐块目检与游戏截图对齐；
- **遗留**：姿态强依赖运行时的部件仍不做——机械臂关节（坐标按 BE pivot 摆放）、
  部署器手、绳索/软管长度、门扇、飞轮/水车（OBJ 已覆盖）、`conveyor_casing.obj`
  （字节码里无对应字段，疑为废弃资源）。

| 指标                          | 第三轮前      | 第三轮后           |
| --------------------------- | --------- | -------------- |
| chain\_conveyor             | 34 面（仅外壳） | 141 面（+轮/轴/护罩） |
| gearshift / clutch          | 16 面      | 28 面（+上下半轴）    |
| gearbox                     | 18 面      | 22 面（+水平半轴）    |
| andesite\_encased\_cogwheel | 20 面      | 64 面（+上下半轴）    |

**动机**：NeoForge 实机验收（Create 6.0.10）暴露——烘焙 2254 个方块后仍有
**56 个方块走"近似几何"兜底**。逐块归类（探针 `dist/_r12_probe.py`，复用烘焙
自身判定逻辑）后，根因**不是"没有模型"，而是三类非常规模型**：

| 类别                  | 数量 | 其中 create | 说明                                                                                                          |
| ------------------- | -- | --------- | ----------------------------------------------------------------------------------------------------------- |
| **A. OBJ 模型**       | 23 | 23        | `loader: neoforge:obj`，几何在 `.obj` + `.mtl` 里                                                                |
| **C. composite 复合** | 18 | 18        | `loader: neoforge:composite`，`children` 是标准 JSON elements                                                   |
| **B. 空几何**          | 14 | 4         | 无 `elements`；原版 9 个（water/lava/barrier/light/…）本无几何，create 4 个中 2 个是流体（chocolate/honey，无害）、2 个是 Flywheel 视觉 |
| **D. 其它**           | 1  | 0         | `minecraft:heavy_core`——烘焙未识别 `"all"`（缺 `#` 前缀）的变量引用                                                        |

**关键实证**（决定三条方向的取舍）：

- OBJ 是 **Blender 导出的标准 OBJ + 配套** **`.mtl`**，顶点坐标**已是 0\~1 方块单位**
  （`v 0.3125` = 5/16 像素），面数小（31 \~ 3236，最大 `waterwheel_large` 181 KB）；
- composite 的 `children.*.elements` 是**完全标准的 JSON 模型元素**
  （`from/to/faces/uv/rotation`），可**直接复用现有** **`build_model_quads`**；
- `BeltVisual extends KineticBlockEntityVisual`，几何由 Flywheel
  `ScrollInstance[] belts` / `RotatingInstance pulley` 描述——**不是** **`ModelPart`**，
  且皮带形状按路径/滚动**运行时生成**（`GaugeVisual` 同理）。

**方向评估结论**：

1. **方向 2「特殊 loader 支持」——推荐优先实施**（原表述"资源侧提取"收窄为
   明确的两件事：① `neoforge:obj` 解析；② `neoforge:composite` 展开）。
   覆盖 **41/45** 个 create 兜底方块（91%；A+C 两类全部拿下），
   **纯静态、零游戏依赖、可自动化差分回归**。
   成本：composite 展开 ≈30 行（几乎白送），OBJ 解析 ≈150\~200 行
   （`v/vt/f` + `mtllib/usemtl` → 贴图映射 + `flip_v` + 多边形转四边形）。
2. **方向 1「运行期导出」——不建议投入**。Flywheel 视觉不基于 `ModelPart`，
   导出需深度集成 Flywheel 1.0.6 内部 API（随 Create/Flywheel 升级即失效），
   且得到的是"某一时刻某一摆放"的快照（皮带长度/坡度随状态变化），
   无法作为通用方块几何；收益仅 2 个方块。
3. **方向 3「语义近似」——保留为兜底**。仅需覆盖剩余 2 个 Flywheel 方块
   （皮带≈薄板、面板表≈小盒），在现有 `--classes` 的 `tex` 之外增加
   "几何模板"（如 `box:4,0,4,12,8,12` 或预设形状），成本极低。

**泛用性（实证，跨加载器差异极大）**：

- **NeoForge 侧：强。** loader 是**平台内置**的——从
  `neoforge-21.1.256-universal.jar` 提取到 5 个：
  `model.obj.ObjLoader`、`model.CompositeModel$Loader`、`model.ElementsModel$Loader`、
  `model.SeparateTransformsModel$Loader`、`model.EmptyModel`。所有 NeoForge 模组
  写 `"loader": "neoforge:obj"` 都是**同一套语义**，参数固定（从 `ObjLoader`
  字节常量提取：`model` / `mtl_override` / `flip_v` / `automatic_culling` /
  `shade_quads` / `emissive_ambient`）→ **一次实现覆盖全 NeoForge 生态**。
  对方块几何有意义的实际只有 `obj` + `composite`（`separate_transforms` 服务于
  物品视角、`empty` 本就无几何、`elements` 语义近似标准 elements）。
- **Fabric 侧：弱。** Fabric API（含 1.21.1 `fabric-model-loading-api-v1`
  2.1.0）**不提供任何内置几何 loader**——源码里只有编程式 API
  （`ModelLoadingPlugin` / `ModelResolver` / `ModelModifier`），
  没有 `ObjLoader`/`CompositeLoader` 之类的类。Fabric 模组要用 OBJ 必须
  **自己在 Java 里写 loader**（私有命名空间与参数）→ 无法通用静态解析，
  只能逐模组适配（或退回方向 1/3）。
- **实测样本分布**：`create` 643 方块 = 标准 602 + `neoforge:obj` 23 +
  `neoforge:composite` 18；`ultramarine` 549 方块（25764 个模型引用）、
  `more-models`、原版 1062 方块 = **100% 标准 elements，零非常规 loader**。
  即：**装饰/建材类模组现有烘焙已完全覆盖**，方向 2 的增益集中在
  **机制/科技类模组**，而这类模组在 NeoForge 生态（Create / Mekanism / IE 系）
  占主流。

**顺带发现（建议随方向 2 一并修）**：

- 烘焙解析面贴图引用时**只认** **`#var`**，遇到 `"all"`（1.21 原版 `heavy_core` 的
  写法，缺 `#`）即丢弃该面 → 退化为兜底；原版解析器对此容错，烘焙需跟上；
- `create:chocolate` / `create:honey` 确认是**流体**（`textures/fluid/*_still.png`
  - `data/c/tags/fluid/*.json`），在 B 类清单中属无害噪音；但**存档模式**的
    Python 侧分类是否同样判为流体，需单独核对。

**验收**：方向 2 落地后，A/C 两类（41 个 create 方块）输出真实几何；
剩余兜底项在烘焙输出中仍明确列出，且仅剩 2 个 Flywheel 视觉方块
（`belt` / `factory_gauge`）与 2 个流体（`chocolate` / `honey`，无害）。

**限制**：Flywheel 系的实例化方块几何本质上是运行时的，静态烘焙无法覆盖；
每引入一个新模组都可能新增非常规 loader，属"长期演进"而非一次性任务。

## N1 NeoForge 适配（21.1.x）\[P1·易] ✅ 已完成

**动机**：控制模式的 MC 端从 Fabric 迁移到 NeoForge（PCL 实例
1.21.1-NeoForge\_21.1.256，带 Create 6.0.10 作为模组方块压力样本）。

**架构**：新工程 `mcmod-neoforge/` 与 `mcmod/` 并存，不动 Fabric 工程。
纯 Java 部分（codec/、mesh 主体、http/Query、core/VersionTracker、tex/）
经 `srcDirs + exclude` **直接引用** Fabric 工程源码（零拷贝、零漂移）；
只有 5 个加载器耦合文件用 **Mojang 官方映射（mojmap）** 重写：
`McBridge`（@Mod + NeoForge.EVENT\_BUS 的 ServerStarted/Stopping）、
`WorldChunkMixin`（LevelChunk，NeoForge 21.1 运行时即 mojmap 名，无需 refmap）、
`ChunkSnapshotService`、`ApiServer`、`BlockClassifier`。

**映射定案**（yarn→mojmap，用 yarn mappings.tiny × Mojang client.txt 交叉
反查，不靠记忆）：`LevelChunk.setBlockState` 1.21.1 仅 boolean 重载（与 yarn
签名一致）；`isOpaqueFullCube` = `canOcclude() && isCollisionShapeFullBlock`；
`getWorlds→getAllLevels`、`getVersion→getServerVersion`、
`Registries→BuiltInRegistries`、`getLuminance→getLightEmission`、
`EulerAngle→Rotations`（getPitch/Yaw/Roll→getX/Y/Z 次序一致）、
`networkHandler.requestTeleport→connection.teleport(...,Set<RelativeMovement>)`、
`ChunkStatus` 移包 `world.level.chunk.status`。

**构建双轨**：CI/正常网络走 ModDevGradle 2.0.148（`gradle build`）；
本地 GFW 环境对 maven.neoforged.net 连接重置且无公共镜像（BMCLAPI 只代理
neoforge 主 jar，NFRT/NeoForm 不可得），新增
`tools/build_neoforge_local.py`——用实例自带的 AutoRenamingTool 把 vanilla
slim.jar 按 Mojang 官方映射（--reverse）重命名作编译类路径，javac 直编
（-proc:none 规避 mixin AP），产物与 MDG 等价（`build_neoforge_local.py`
输出同路径 `build/libs/mcbridge-neoforge-<ver>.jar`，pack\_release 可直接取）。
实证：用户实例日志显示 Flywheel 的 LevelChunkMixin 以 mojmap 名混入，
运行时映射体系确认无误。

**验收**：jar 已部署实例 mods/；进世界后日志出现 API 已启动；
`curl /api/ping /api/blocks /api/chunk /api/worldrev` 正常；
游戏内放置/拆除方块 worldrev 递增（mixin 生效）。

## R17 运行时几何导出（多模组兼容的系统解）\[P1·难] ✅ 已完成（全链路落地；R17b Flywheel 视觉层）

**动机**：R12 方向 2/3 靠"读字节码找部件 + 手写朝向"逐块补，用户连续三轮反馈
"补了又缺"。根因是结构性的：Create 这类科技模组的机器几何**由渲染器在运行时
生成**（Flywheel / BlockEntityRenderer），资源侧只留下不被 blockstate 引用的
"孤儿模型"，且**姿态取决于运行时状态**（邻居连接、动力网络、转动相位、内容物）。
量化后（`dist/_r16_coverage.py`）：Create 643 方块中静态完整 582、**需运行时部件
54**、空模型 7——人工表覆盖不到后者的一半。

**方案**：不改"逐块理解代码"，改为**让游戏自己把几何吐出来**——完整设计与取舍见
[docs/运行时几何导出方案.md](运行时几何导出方案.md)。核心是三条：

1. 注入点选 `MultiBufferSource.BufferSource#getBuffer`（所有世界几何汇入 GPU 前的
   共同咽喉，且是**纯原版类**）：不需要依赖 Create/Flywheel 的编译期 API，
   不随模组升级失效——这比 R12 里评估过的"深度集成 Flywheel 内部 API"稳得多；
2. 捕获是 **tee（旁路）**：顶点照常转发给真身，画面不受影响；按**空间盒**筛目标
   方块，顺带判定"提交坐标是世界坐标还是相机相对坐标"；
3. 触发走**已有 HTTP API**（`/api/capture`、`/api/capture/result`）而非客户端命令/
   按键——复用现有基础设施，且便于脚本批量驱动（遍历方块 × 规范布局）。

**已实现（全链路落地）**：

| 文件                                      | 职责                                                                          |
| --------------------------------------- | --------------------------------------------------------------------------- |
| `capture/GeometryCapture.java`          | 状态 / 统计 / 报表；按方块归属的 sink；**刻意不引用客户端类**，服务端侧可安全加载                            |
| `capture/QuadRecorder.java`             | 客户端 tee 型 `VertexConsumer`（累积顶点 → 四边形）+ 建图集 sprite 表                        |
| `mixin/MultiBufferSourceMixin.java`     | 客户端 mixin，挂在 `getBuffer` 的 RETURN                                           |
| `capture/FlywheelCapture.java`          | **R17b**：Flywheel 视觉层取顶点（`Model#meshes` → `Mesh#write`）+ 反射读实例变换 + 读渲染原点    |
| `mixin/BlockEntityStorageMixin.java`    | **R17b**：视觉构造时置「当前方块」上下文                                                    |
| `mixin/InstancerProviderImplMixin.java` | **R17b**：记录 `(方块, Model, InstanceType)`，跳过 SHADOW                           |
| `mixin/MinecraftTickMixin.java`         | **R17b**：帧末在捕获窗口内做一次快照                                                      |
| `http/ApiServer.java`                   | 新增 `/api/capture`、`/api/capture/result`、`/api/capture/sprites`、`/api/block` |
| `mcbridge.mixins.json`                  | 记录层 mixin 放入 `client` 数组（专用服务端不加载）                                          |
| `tools/capture_export.py`               | 批量导出：归属 → 纹理还原 → 按状态聚合 → `dist/runtime_models.json`                         |
| `bake_assets.py --runtime-models`       | 按 `(方块, 属性集)` 把捕获几何挂到对应状态，与静态底座**合并**                                       |

构建侧：`tools/build_neoforge_local.py` 补了 brigadier 到编译类路径，并让 javac
用英文报错；`Dist` 在运行时不在任何库 jar 里（只在构建工具 jar），故**放弃
`@EventBusSubscriber`** **的 side 过滤**，改用 HTTP 触发——顺带省掉客户端命令 API 依赖。

**✅ 原型验证已通过（实机 Create 6.0.10 基地）**：

| 结论      | 证据                                                                                                                                                                                                                                                                                              |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 拦截点有效   | 报告里出现 `entity_cutout` / `entity_solid` / `entity_translucent_cull` / `solid` 四层，全部经过 `BufferSource`                                                                                                                                                                                             |
| 机器几何抓得到 | 在坐标 (125.5, −59.5, −387.5)（该区块含 12 种 create 方块：belt / chain\_conveyor / crushing\_wheel / mechanical\_drill / mechanical\_mixer / mechanical\_press / speedometer / stressometer …）抓到 `entity_cutout` **3204 条**、`entity_translucent_cull` **14418 条**；两者都用 **blocks.png 图集**（不是生物图集）→ 是方块实体层几何 |
| 坐标系已判定  | 命中全部走「相机相对」分支（`hitWorld` 恒为 0）→ **提交坐标 = 世界坐标 − 相机位置**，后续切方块局部空间需先加相机                                                                                                                                                                                                                           |
| 需要手部过滤  | 第一人称手/手持物也在 `entity_cutout` 里且贴近相机，会污染小半径捕获 → 已加"距相机 1.5 格内剔除"（工具侧）                                                                                                                                                                                                                             |

新增工具：`tools/capture_probe.py`（定点捕获 + 报表汇总）、`tools/capture_locate.py`
（大半径扫描 + 命中聚类，自动定位"哪里有运行期几何"，免人工报坐标）。

**进展（按顺序）**：

1. ~~跑原型确认 Flywheel 几何可捕获~~ ✅；
2. ~~UV 反查图集 sprite → 还原源贴图路径，接回烘焙端~~ ✅（sprite 表）；
3. ~~捕获实例变换（旋转/平移）~~ ✅（对照 Create/Flywheel 着色器 + 读渲染原点 + 法线同转）；
4. ~~实机基地跑通：25 状态 / 13 种机器进入~~ ~~`assets.mcba`~~ ✅；
5. **待办**：规范布局批量导出（孤立 / 直线 / 成对）以扩大覆盖 + 可复现；个别部件的面级 UV 核对；
   用第三方模组（如实例里的 `ultramarine`，独立建筑模组）**实证**兼容边界。

**已知限制**：姿态依赖运行状态者只能给"捕获瞬间的姿态"（原理限制，非工具缺陷）；需要真实
客户端与 GPU；单人游戏（客户端与集成服务端同 JVM）为前提。**模组兼容边界**见
[docs/运行时几何导出方案.md §11](运行时几何导出方案.md)。

**与 R12 方向 3 的关系**：`_BE_PARTS` 保留——纯资源侧、零游戏依赖、可回归，
对"姿态固定"的部件仍是最快路径；R17 是其补集。

## 已知缺陷修复追踪

| 编号  | 问题                                                                                        | 状态    |
| --- | ----------------------------------------------------------------------------------------- | ----- |
| F1  | 存档模式 palette 需支持 compound 格式                                                              | ✅ 已修复 |
| F2  | 1.16+ PalettedContainer 位流不跨 long 边界                                                      | ✅ 已修复 |
| F3  | MCM1 向量化解码 stride 错位                                                                      | ✅ 已修复 |
| F4  | BYTE\_COLOR 顶点色写 uint8 被钳制为白色（AO/染色失效）                                                    | ✅ 已修复 |
| F5  | 植物方块叠加整方块且未染色（模型判定 + 掩码位序不一致）                                                             | ✅ 已修复 |
| F6  | 模组方块 UV 误按 texture\_size 归一化（应为 /16）                                                      | ✅ 已修复 |
| F7  | 群系染色整体斜移一格（`_quad_cells` 的 u/v 轴漏 +1）                                                     | ✅ 已修复 |
| F8  | 烘焙模型面按 floor/ceil 定格，未按面法向（贴边界薄面错一格）                                                      | ✅ 已修复 |
| F9  | 顶点色按 sRGB 值写入 BYTE\_COLOR（`.color` 是线性）→ 染色/AO 被整体提亮约 2.9 倍，观感"偏灰"                        | ✅ 已修复 |
| F10 | 草方块侧面 overlay 与基底共面 → Z-Fighting，草皮那层被基底盖住（侧面草皮显示成基底里的平原绿）                                | ✅ 已修复 |
| F11 | 活塞/熔炉/工作台等"几何占满整格但**有朝向**"的方块被误判为简单立方体，走程序化立方体路径 → 朝向与正面贴图全部丢失（活塞头贴图跑到错误的面上，水平放置时"UV 错乱"） | ✅ 已修复 |

> F7：`_emit` 里 u/v 轴写的是 `u0 - 1`（格子索引 − 1），故 u/v 格 = 顶点 + 1；
> 漏掉该 +1 会让群系查找整体斜移 (−1,−1)，表现为"颜色与群系位置对不上、边界错一格"。
> 表面上看不出来的原因是：出图脚本与审计脚本都调用同一个函数，自洽地掩盖了它。
> 修复后以 `/fillbiome` 矩形为外部真值逐面对拍：206 + 50 面全部匹配、0 失配。
>
> F8：模型顶点是"以自身方块原点"表达的 0..16 像素，面心落在格边界时归属取决于
> 面法向（+dir 面在格上边、-dir 面在格下边，两者都属本方块）；只按 floor/ceil
> 会把其中一半判到隔壁格，草方块/树叶的 −X/−Z 侧面因此取到邻格群系色。
>
> F9：`MeshLoopColor.color` 是**场景线性**空间，而 vcol 是按 sRGB 空间算的
> uint8（tint × AO）。直接写 `byte/255` 等于把 sRGB 值当线性用 —— 标定实验
> （`dist/_vcol_calib.py`：已知灰贴图 × 已知顶点色，顶光 π）实测 92 而正确值 60，
> 判定为线性解释。修法是写入前做 sRGB→线性。修复后顶视实测沼泽草
> (63,67,31) / 沙漠草 (117,112,49)，与原版应有色 (61,65,33) / (110,106,49) 一致。
> 同一实验也说明 GUI 观感"偏灰"是两件事叠加：顶点色被提亮 + 启动脚本的
> 蓝灰环境光（strength 1.2）把顶面推到 2 倍以上；两者都已修。
>
> F10：MC 的草方块由**两个元素**构成（基底 `grass_block_side` + `overlay`
> `grass_block_side_overlay`，后者带 tintindex）。两个问题叠在一起：
> ①`mesh_padded` 原先按运行时 class 跳过模型注入（`ecls == OPAQUE`），而草方块在
> 共享表里正是 OPAQUE，于是整模型被跳过、只走整立方体路径 —— 那条路径每种面组
> 只有一张贴图，侧面因此只剩基底贴图（里面烘焙了平原绿），草皮层完全丢失；
> ②两个元素坐标完全相同 → 共面，原版靠分层绘制顺序 + LEQUAL 决胜，Blender 没有
> 这个次序 → Z-Fighting。
>
> 修法：注入条件只看资产包的 `use_model`；对「整立方体 + 叠加层」结构**不新增任何
> 几何**，改由材质在**着色器里把两层合并**（`mats._wire_overlay`）：
>
> ```
> final = [ base x (1 - ov.a) + ov.rgb x ov.a x tint ] x AO
> ```
>
> 顶点色因此改成 `rgb = tint x AO`、`a = AO`（AO 单独一份，供基底部分使用，
> 也正是 `设计方案.md` 原本的设计）。tint 仍来自顶点色 ⇒ 仍随群系变化，而基底
> （泥土）不会被染色。判据 `AssetPack.overlay_faces()` 要求"去重后是完整 6 面立方体
>
> - 叠加层 UV 与基底一致 + 叠加层覆盖该面组的全部方向"，否则退回整模型注入
>   （如 `spawner` 的内外 6 层）。
>
> 实测：草密集区块 面数 1026（与完全没有叠加层的基线**完全相同**，零额外几何），
> 材质 +1；受控实验（`dist/_overlay_calib.py`，平面铺 1×1 草侧面 + 顶光 π）
> 实测 sRGB (118.2, 88.5, 59.1) vs 解析 (124.7, 91.6, 62.9)，与上式一致。

## v1.3.1 修复记录（R8 控制模式实机验收）

> 控制模式接真实世界（含 Ultramarine 模组 518 方块）实测暴露的一批问题，
> 全部有实况数据/字节码取证，非猜测修复。

- **方块遮挡判据**：BlockClassifier 原用反射按 Yarn 方法名探测遮挡 —— 发布包
  经 loom 重映射后方法名为 intermediary，反射必然失败，整片掉进"名字猜不到 ->
  OPAQUE"兜底：所有非整方块（箱子/雪层/花盆/模组花瓶…）错误遮挡邻居。
  改为 isOpaqueFullCube（= 原版面剔除同口径：不透明 && 剔除形状是完整立方体），
  任意原版/模组方块通用正确；缓存按 BlockState 恒等键（雪层等每层状态形状不同）。
  验证：gradle runServer + /api/blocks 全注册表对拍。
- **模型元素级 rotation 三处错误**（凳腿散架 / 瓦当斜面填满整格）：
  对照 BakedQuadFactory.rotateVertex 字节码 —— ①角度应为右手系**正角**（旧实现
  镜像）；②rescale 未实现（垂直两轴 x 1/cos(|angle|)，旋转后缩放）；
  ③int(round(22.5)) 银行家舍入成 22 度。注意原版两套符号相反：blockstate 级
  x/y 旋转为负角（TestVariantRotation 7 项对拍为证），元素级为正角。
- **状态属性序列化名**：stateName() 属性值反射 asString() —— 与遮挡判据同类
  的重映射问题，模组自定义枚举退化为 toString() 大写（snow\_side=NONE），资产包
  规则是小写 -> 变体不命中 -> 整方块（离线扫描走存档 NBT 全是小写，故从未暴露；
  用玩家实况 /api/chunk 普查才抓到）。改用原版 Property.name(value)。
- **water\_cauldron 误判流体**：兜底按"名字含 water"猜液体 -> 含水炼药锅整个被
  替换成流体几何。改为 instanceof FluidBlock 精确判定。
- **LOD 分层移除**（用户决策）：所有距离统一满质量网格，消除远看方块化/拉近
  重建的闪变；lod1\_dist/lod2\_dist 面板项随之移除（协议保留 lod 参数）。
- **AO 不再烘焙**（用户决策）：Blender 自有光照；合并键少一个 AO 维度，
  矩形拆分更少。
- **性能**：\_fluid\_quads 只在液体格 +-1 包围盒内计算（内容顶到 ymax 的体积
  138ms -> 3ms）；贪心平面内按键分组向量化 + 包围盒内提矩形 + \_rects 位运算
  （单组 158\~169ms -> 118ms，输出与旧实现逐位一致，A/B 对拍与 Java 夹具不受
  影响）；Blender 工作线程 GIL 争抢用 setswitchinterval(0.001) 压制（主线程
  50ms tick 最大漂移 69.6ms -> 33.1ms）。
- **MCC1 v2**：Section 附带 4x4x4 群系（名表 + 64 下标），控制模式本地网格与
  存档模式共用同一条按群系染色链路；A/B 一致性测试恢复（904 != 909 -> 相等）。

