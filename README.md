# mc-blender-bridge

让 Blender 借助**本地运行的 Minecraft Java 版实例**或**直接读取存档文件**，
按相机位置**动态加载/卸载区块**，并在数据链路中完成**面剔除、贪心合并、
AO 烘焙与 LOD 降级**的桥接系统。

替代「Mineways/jMC2Obj 全量导出 → Blender 一次性导入」的静态管线：
场景再大，Blender 内存占用只与**镜头周围的加载半径**相关，与地图总规模无关。

```
┌──────────────── Blender (Python 插件) ─────────────────┐
│ N面板 ─ 调度器(迟滞半径/优先级) ─ 工作线程                │
│   控制模式: HTTP 拉取(keep-alive)                        │
│   存档模式: 直接解析 Anvil 存档(NBT/Region)               │
│   主线程分帧应用(foreach_set) · 网格缓存 · LRU卸载 · LOD  │
└──────────┬──────────────────▲─────────────────────────┘
           │                  │
┌──────────▼─────────┐ ┌──────┴───────────────────────────┐
│ 控制模式数据源       │ │ 存档模式数据源                    │
│ 本地 MC Java 实例    │ │ 存档目录 region/*.mca + level.dat │
│ (Fabric 模组/sim)   │ │ 无需 MC 运行，零依赖              │
└─────────────────────┘ └──────────────────────────────────┘
```

## 两种加载模式

| | 控制模式（control） | 存档模式（save） |
|---|---|---|
| 数据源 | 运行中的 MC 实例（Fabric 模组）或 `server_sim` | 存档文件（`region/*.mca`） |
| 是否需要 MC 运行 | 需要 | **不需要，甚至无需安装 MC 模组** |
| 世界改动实时预览 | 支持（版本轮询） | 不支持（静态快照） |
| 适用 | 游戏/录制同步、边改边渲染 | 离线渲染、旧地图取景、无 MC 环境 |

两种模式共用同一套调度器：迟滞装卸、距离优先队列、LOD 三档、分帧应用。

## 场景组织（根空物体）

所有区块对象与相机统一挂在根空物体 **`MCB_Root`** 下：

- 区块按根物体**局部坐标**放置——整体移动/旋转/缩放根物体，全部区块与相机同步变换；
- 加载判定使用**相机相对根物体的位置**，根物体自身变换不触发装卸；
- 连接时自动把场景相机挂到根节点（可关），也可手动执行「绑定相机到根节点」。

## 原版资产包（贴图 / 模型烘焙）

装一次资产包，即可获得**原版贴图**与**真实模型形状**（楼梯阶梯、栅栏十字、
门/活板门、玻璃板…），控制模式与存档模式通用：

```bash
pip install pillow   # 仅烘焙工具需要
python tools/bake_assets.py "<客户端 jar 或资源包 zip>" "mods/*.jar" -o dist/assets.mcba --mc-version 1.21.1
# 客户端 jar 示例：.minecraft/versions/1.21.1-Fabric_0.17.2/1.21.1-Fabric_0.17.2.jar
# 模组方块：把 mods/*.jar 一并作为输入即可（自动扫描各命名空间）
#   PowerShell / cmd 不会替原生命令展开 *，加引号交给脚本展开即可（如上）
```

N 面板 > MC Bridge > 资产包 > 选择 `assets.mcba` > 加载。加载后：

- 贴图优先级：**资产包 > 服务端 `/api/texture` > 程序化色块**；
- 顶点色 = 逐面染色（草顶染绿等）× AO；
- 非整立方体方块按 `facing/half/shape` 等状态注入烘焙模型几何（真实 UV + cullface）；
- 控制模式 LOD0（近处）自动走本地网格以注入模型，LOD1/2 仍用服务端网格保吞吐；
  「烘焙模型」开关可关闭。

> 方块实体（箱子/床/告示牌/旗帜/潜影盒/头颅/装饰陶罐/传送门框架/钟等）没有
> JSON 几何（由 Java 渲染器绘制），改用**从客户端 jar 提取的原版模型**：
> `tools/dump_be_models.ps1` 直接调用原版模型工厂导出顶点/UV/面法线
> （`tools/vanilla_be_models.json`），烘焙时再套用原版渲染器的矩阵变换
> （朝向/贴图/部件显隐），因此几何与原版逐顶点一致。
>
> 重新提取（换 MC 版本或首次克隆后缺失该 JSON 时）：
> ```powershell
> powershell -ExecutionPolicy Bypass -File tools/dump_be_models.ps1
> ```
> 需要 JDK（`JAVA_HOME`）与 loom 缓存中的客户端 jar；未收录的方块（含模组方块
> 实体）自动退回简化代理模型。

![资产包渲染预览（真实贴图 + 楼梯/栅栏/门模型）](docs/img/r2_preview.png)

## 仓库结构

| 目录 | 内容 | 状态 |
|---|---|---|
| `blender_addon/mc_bridge/` | Blender 插件（bpy 层 + 纯 Python 核心） | ✅ 已实现并测试 |
| `server_sim/` | MC 模拟服务器（无 MC 环境的开发/测试替身，与模组 API 同构） | ✅ 已实现并测试 |
| `mcmod/` | Fabric 服务端模组（Java） | ✅ 源码完整；纯 Java 部分已与 Python 逐字节对拍 |
| `tests/` | 95 项 Python 测试 + 跨语言夹具 | ✅ 全部通过 |
| `docs/` | 设计方案 / 协议规范 / 用户手册 / 路线图 / 预览图 | ✅ |
| `tools/` | 资产烘焙、夹具生成、预览导出、Java 合并验证脚本 | ✅ |

## 快速开始（无 Minecraft，30 秒体验）

```bash
# 终端 1：模拟服务器（生成确定性世界）
python3 server_sim/mc_server_sim.py --port 8788

# 终端 2：Blender
#   编辑 > 偏好设置 > 插件 > 安装 -> 选择 blender_addon 目录打包的 zip
#   （开发模式：把 blender_addon/mc_bridge 软链到 Blender addons 目录）
#   N 面板 > MC Bridge > 加载模式=控制模式 > 主机 127.0.0.1 端口 8788 > 连接
#   相机移到任意位置，区块自动流入；播放动画时随帧装卸
```

## 快速开始（存档模式，无需 MC 运行）

1. N 面板 > MC Bridge > 加载模式切到 **存档模式**；
2. 选择存档目录（含 `region/` 与 `level.dat`，主存档在
   `.minecraft/saves/<世界名>/`，服务端在 `world/`）；
3. 连接。区块按相机位置动态装卸，材质使用内置程序化贴图。

支持主世界 / 下界 / 末地（维度下拉跟随 `dim` 属性）；
兼容 1.13+ 存档（1.16+ 与 1.15- 两种调色板位压缩自动识别）。

## 快速开始（真实 Minecraft）

```bash
# 1. 构建模组（需 JDK 17+ 与网络）
cd mcmod && gradle build  # 需 Gradle 8+（或用 IDE 打开 mcmod 执行）
# 产物: build/libs/mcbridge-1.0.0.jar

# 2. 服务端安装（推荐独立服务端，无暂停问题；单人模式请"对局域网开放"）
cp mcbridge-1.0.0.jar <服务端>/mods/
# 启动服务端，确认日志: "MC Bridge API 已启动: http://127.0.0.1:8788"

# 3. Blender 插件连接 127.0.0.1:8788（默认端口）
```

## 测试

```bash
# Python 全量（95 项：编解码/网格器/调度器/存档解析/插件冒烟/端到端/原版对拍）
python3 -m pytest tests/

# 跨语言一致性（Java vs Python 夹具逐字节对拍，仅需 JRE）
python3 tools/merge_verify.py
cd mcmod/build/verify && java -Dfixtures=../../tests/fixtures ConformanceVerify.java

# 一致性夹具再生成（改算法后）
python3 tools/gen_fixtures.py

# 预览导出（需先启动 server_sim）
python3 tools/export_preview.py   # -> docs/img/preview.obj + preview.png
```

## 性能（R=4 共 49 区块，真实 Blender 4.5 实测）

| 场景 | 首载 | 热缓存（LOD 切换/回看） |
|---|---|---|
| 控制模式（server_sim） | ~2.2s | **~0.2s** |
| 控制模式 raw（客户端网格） | ~1.5s | ~1.5s |
| 存档模式 | 首载后受区块解析缓存加速 | — |

关键手段：HTTP keep-alive 长连接、3×3 邻域请求合并（在途去重）、
服务端/客户端网格结果 LRU、解码与几何装配全向量化、
主线程 `foreach_set` 建网格、双预算分帧应用。

## 关键设计（详见 docs/设计方案.md）

- **减面**：邻接剔除矩阵（六类方块）+ 贪心矩形合并（方向/方块/AO 四元组约束），
  实测单区块减面 60–95%；AO 以顶点色×贴图节点接入 EEVEE/Cycles。
- **UV 平铺**：贪心合并的大四边形用「块单位 UV + 贴图 Repeat」，
  每方块独立材质——正确性与减面兼得。
- **调度**：加载/卸载双环迟滞（防镜头抖动反复装卸）、距离优先队列、
  每帧限量应用、区块版本号低频轮询（控制模式游戏内边改边预览）。
- **存档解析**：NBT 大端解析 + Region 扇区惰性读取 + 调色板位压缩向量化解码，
  区块 payload 与网格产物双 LRU。
- **确定性渲染**：预热模式（遍历帧范围收集区块并集）与烘焙静态化
  （导出 .blend 库，兼容无 MC 的渲染农场）。
- **一致性**：Python 参考实现 ↔ Java 实现 通过夹具逐字节对拍；
  模式 A（Blender 本地网格）与模式 B（服务端网格）逐面一致。

## 当前限制

- 资产包覆盖**所有传入 jar 的命名空间**（原版 + 模组）：把 `mods/*.jar`
  一并烘焙即可获得模组方块的真实贴图与 JSON 模型；未传入的模组方块仍回退为
  完整方块近似。动态 BlockState/BakedModel（代码模型，非 JSON）同样回退近似。
- 无资产包时非完整方块（楼梯/栅栏等）按完整方块近似，贴图为程序化色块。
- 流体（水/岩浆）在**本地网格路径**（存档模式、`mode=raw`）按原版方式渲染：
  水面高度 = 流体 level/9（水源 8/9；**方块状态 `level` 与流体 level 相反**——
  `level=1` 最靠近水源最厚 7/9，`level=7` 最远最薄 1/9）、四角按邻居高度平滑、
  同类流体互不生成面；
  上方仍是同种流体时侧面为整格竖直面（原版 `FluidRenderer` 的 `own >= 1.0` 分支）；
  走服务端网格（`mode=mesh`，MCM1 整型块坐标）时仍为整方块。
  逐条规则见 [docs/设计方案.md](docs/设计方案.md) §5.7。
  注：岩浆的液体分类来自资产包（`bake_assets` 的 `water/lava` 启发式）；
  未加载资产包时共享小表只收录水，岩浆会退回不透明整方块。淡水体（海/湖/含水层）
  与含水方块不受此限。
- 实体/方块实体不在范围内。
- 存档模式只读（不能像控制模式那样把玩家传送到相机位置）；
  自定义数据包维度需手动适配 ymin/height。
- MC 模组按 Yarn 1.20.1 编写： Mixin 注入失败不影响启动（版本追踪退化为不可用）；
  其他 MC 版本需调整映射。

## 未来规划

见 [docs/roadmap.md](docs/roadmap.md)（R1 高度轴分区块、R3 模组资产烘焙、
R4 跨区块合并、R5 实体、R6 渲染增强、R7 生态）。
