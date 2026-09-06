#!/usr/bin/env python3
"""把无 Minecraft 依赖的纯 Java 部分合并为单文件源码，供 JRE 单文件启动运行一致性对拍。
（沙箱只有 JRE 没有 javac；真实环境用 `./gradlew test`。）"""
import re
import sys

order = [
    "src/test/java/com/zcube/mcbridge/ConformanceTest.java",
    "src/main/java/com/zcube/mcbridge/codec/BytesLE.java",
    "src/main/java/com/zcube/mcbridge/mesh/BlockClass.java",
    "src/main/java/com/zcube/mcbridge/codec/Payloads.java",
    "src/main/java/com/zcube/mcbridge/codec/Mcc1Writer.java",
    "src/main/java/com/zcube/mcbridge/codec/Mcm1Writer.java",
    "src/main/java/com/zcube/mcbridge/mesh/GreedyMesher.java",
]
parts = []
for f in order:
    src = open(f, encoding="utf-8").read()
    body = re.sub(r"^package .*?;\s*", "", src, flags=re.M)
    body = re.sub(r"^import .*?;\s*", "", body, flags=re.M)
    parts.append(body.strip())
merged = "\n\n".join(parts)
for cls in ("BytesLE", "BlockClass", "Payloads", "Mcc1Writer", "Mcm1Writer", "GreedyMesher"):
    merged = merged.replace(f"public final class {cls}", f"final class {cls}")
for nested in ("ChunkPayload", "PalEntry", "SectionData", "Quad"):
    merged = re.sub(rf"(?<![\w.]){nested}\b", f"Payloads.{nested}", merged)
merged = merged.replace("public record Payloads.ChunkPayload", "public record ChunkPayload")
merged = merged.replace("public record Payloads.PalEntry", "public record PalEntry")
merged = merged.replace("public record Payloads.SectionData", "public record SectionData")
merged = merged.replace("public static final class Payloads.Quad", "public static final class Quad")
merged = merged.replace("public Payloads.Quad(short[] verts", "public Quad(short[] verts")
merged = merged.replace("public final class ConformanceTest", "public class ConformanceTest")
header = ("import java.io.ByteArrayOutputStream;\nimport java.io.IOException;\n"
          "import java.nio.charset.StandardCharsets;\nimport java.nio.file.Files;\n"
          "import java.nio.file.Path;\nimport java.util.ArrayList;\n"
          "import java.util.Arrays;\nimport java.util.List;\n"
          "import java.util.TreeMap;\nimport java.util.TreeSet;\n\n")
out = header + merged
path = sys.argv[1] if len(sys.argv) > 1 else "mcmod/build/verify/ConformanceVerify.java"
open(path, "w", encoding="utf-8").write(out)
print("merged ->", path)
