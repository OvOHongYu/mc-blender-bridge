$ErrorActionPreference = "Stop"
$root = $PWD.Path
$toolsDir = Join-Path $root "tools"
$beDir = Join-Path $toolsDir "be_models"
$outDir = Join-Path $beDir "out"

$mc = (Get-ChildItem -Path (Join-Path $root "mcmod\.gradle\loom-cache\minecraftMaven") -Recurse -Filter "minecraft-merged-*.jar" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "sources|backup" } | Select-Object -First 1).FullName
if (-not $mc) { throw "Minecraft jar not found under mcmod/.gradle/loom-cache/minecraftMaven" }

$base = Join-Path $env:USERPROFILE ".gradle\caches\modules-2\files-2.1"
$deps = Get-ChildItem -Path $base -Recurse -Filter "*.jar" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch "sources|javadoc|linux-|windows-|macos-" } | Select-Object -ExpandProperty FullName
$fab = Join-Path $root "mcmod\build\loom-cache\remapped_working"
$cp = (@($mc) + $deps + @((Join-Path $fab "*"))) -join ";"

New-Item -ItemType Directory -Force -Path $outDir | Out-Null
& "$env:JAVA_HOME\bin\javac.exe" -proc:none -cp $cp -d $outDir (Join-Path $beDir "DumpModels.java")
if ($LASTEXITCODE -ne 0) { throw "javac failed" }
$json = Join-Path $toolsDir "vanilla_be_models.json"
& "$env:JAVA_HOME\bin\java.exe" -cp ($cp + ";" + $outDir) DumpModels $json
if ($LASTEXITCODE -ne 0) { throw "java failed" }
Write-Output ("wrote " + $json)

# 实体分层模型（R5：盔甲架等，运行时按 Pose 装配）
$entDir = Join-Path $beDir "out_entity"
New-Item -ItemType Directory -Force -Path $entDir | Out-Null
& "$env:JAVA_HOME\bin\javac.exe" -proc:none -cp $cp -d $entDir (Join-Path $beDir "DumpEntityModels.java")
if ($LASTEXITCODE -ne 0) { throw "javac (entity) failed" }
$entJson = Join-Path $toolsDir "vanilla_entity_models.json"
& "$env:JAVA_HOME\bin\java.exe" -cp ($cp + ";" + $entDir) DumpEntityModels $entJson
if ($LASTEXITCODE -ne 0) { throw "java (entity) failed" }
Write-Output ("wrote " + $entJson)