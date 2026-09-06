package com.zcube.mcbridge.mesh;

/** 方块分类常量（与 Python core/blocks.py、协议规范一致）。 */
public final class BlockClass {
    public static final int AIR = 0;
    public static final int OPAQUE = 1;
    public static final int TRANSPARENT = 2;   // 玻璃/冰：同类相邻面剔除
    public static final int LIQUID = 3;        // 水/岩浆：同类相邻面剔除
    public static final int CUTOUT = 4;        // 树叶/花草：不遮挡任何面
    public static final int NONCUBE = 5;       // 楼梯/台阶等：完整方块近似

    private BlockClass() {
    }
}
