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
> ⑭ **`uvlock` 的旋转方向错了、且漏改面的 rotation 索引**——正确式为
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
  游戏环境验证；存档模式与本地网格路径已按状态解析。

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

## R5 实体与方块实体 [P2·难] ✅ 基本完成（v1.3）

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
> - 画的**逐块定位**未与原版对拍：现按 `Pos` = 画面**底边中点**（MC 实体原点
>   在包围盒底面中心的通用约定），沿 `facing` 外移 1/32 方块（原版画与墙之间
>   确有缝）落位。`facing` 取值与变体尺寸可查证；**未能查证**的是 Pos 的水平
>   参考点与该 1/32 偏移量——1.21.4 及以前画的位置写在 `TileX/TileY/TileZ`、
>   1.21.5+ 改为 `block_pos`，且两者语义有差（Mojira MC-295759 记录了旧存档
>   迁移时"画错一格"）。**对拍工具已就绪**：`tools/paintings_report.py` 遍历
>   存档的画实体，输出原始 NBT 字段 + 按本实现计算的几何（尺寸/朝向/中心），
>   在真实存档放置不同尺寸/朝向的画后运行并与游戏画面核对即可定案。
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
> `net.py::ApiClient.entities` 与 server_sim 已接，调度器据此在 LOD0 把实体
> 并入组对象。**待真实游戏环境验证**（本环境无法编译/运行 MC 模组，
> 需用户构建模组并加载资产包后确认控制模式出画/盔甲架）。
>
> 作物生长**不需要额外工作**：`wheat[age=N]` 等本就是普通 blockstate 变体，
> 由 v3 状态粒度 + 按状态注入烘焙模型覆盖，故"作物生长状态驱动的贴图变体"
> 一项据此结项。

## R6 渲染增强 [P2·易]

- 发光方块（萤石/岩浆）输出自发光节点（Emission × 原版 light 值）。

## R7 稳定性与生态 [P1·易]

- `load_post` 残留对象处理（提示转静态或清除，当前仅有手动卸载）；
- 进度条 UI（预热/首载时显示区块 x/y）；
- CI：GitHub Actions 跑 pytest + Java 对拍 + 打包发布（zip/jar artifact）。

## R8 群系染色（biome tint）\[P1·难]

**存档模式：✅ 已完成** · 控制模式：待做

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
- plains/forest/swamp/desert/snowy_plains/jungle/taiga/dark_forest **八色命中 wiki 真值**；
- 真实存档端到端：swamp `#6A7039`、desert `#BFB755`、plains `#91BD59` 三色精确命中，
  且与 `blocks.py` 原有的"平原常量"完全自洽（平原区颜色不变）；
- 几何代价：真实 fillbiome 布局 +0.05%；密集群系最坏 +20.8%，
  "只让染色方块进键"可压到 **+1.3~1.6%**；
- 回归测试 `tests/test_biome_tint.py`（20 例）。

**剩余（控制模式）**：需扩模组 API 把 biome 随区块下发（协议加 biome 段）；
当前控制模式仍走常量色。

## 已知缺陷修复追踪

| 编号 | 问题                                     | 状态    |
| -- | -------------------------------------- | ----- |
| F1 | 存档模式 palette 需支持 compound 格式           | ✅ 已修复 |
| F2 | 1.16+ PalettedContainer 位流不跨 long 边界   | ✅ 已修复 |
| F3 | MCM1 向量化解码 stride 错位                   | ✅ 已修复 |
| F4 | BYTE\_COLOR 顶点色写 uint8 被钳制为白色（AO/染色失效） | ✅ 已修复 |
| F5 | 植物方块叠加整方块且未染色（模型判定 + 掩码位序不一致）          | ✅ 已修复 |
| F6 | 模组方块 UV 误按 texture\_size 归一化（应为 /16）   | ✅ 已修复 |
| F7 | 群系染色整体斜移一格（`_quad_cells` 的 u/v 轴漏 +1） | ✅ 已修复 |
| F8 | 烘焙模型面按 floor/ceil 定格，未按面法向（贴边界薄面错一格） | ✅ 已修复 |

> F7：`_emit` 里 u/v 轴写的是 `u0 - 1`（格子索引 − 1），故 u/v 格 = 顶点 + 1；
> 漏掉该 +1 会让群系查找整体斜移 (−1,−1)，表现为"颜色与群系位置对不上、边界错一格"。
> 表面上看不出来的原因是：出图脚本与审计脚本都调用同一个函数，自洽地掩盖了它。
> 修复后以 `/fillbiome` 矩形为外部真值逐面对拍：206 + 50 面全部匹配、0 失配。
>
> F8：模型顶点是"以自身方块原点"表达的 0..16 像素，面心落在格边界时归属取决于
> 面法向（+dir 面在格上边、-dir 面在格下边，两者都属本方块）；只按 floor/ceil
> 会把其中一半判到隔壁格，草方块/树叶的 −X/−Z 侧面因此取到邻格群系色。

