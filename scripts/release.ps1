#Requires -Version 5.1
<#
.SYNOPSIS
    打 Windows 分发用的 zip 包（1.0.0 公开分发批次）。

.DESCRIPTION
    四步：
      1. 读版本号：`<python> -c "import mla; print(mla.__version__)"`；
      2. 调 `scripts\build_exe.ps1` 重建 `dist\mla\`（内含 npm build + PyInstaller，几分钟）；
      3. 把 `dist\mla\` 压成 `dist\mla-<version>-windows-x64.zip`：
         用 ZipArchive 逐个写入、条目名统一用 `/`（顶层 `mla/`，解压后是 `mla\mla.exe`）；
         Compress-Archive 在 PowerShell 5.1 下写反斜杠分隔符，跨平台解压会坏，故不用它；
      4. 算 SHA256，写 `dist\mla-<version>-windows-x64.zip.sha256`，
         格式 `<哈希>  <文件名>`（两个空格，和 sha256sum / certutil 对得上）。

    只产出本地文件：不 git tag、不 push、不建 GitHub Release。
    代码签名还没申请，用户首次下载会看到 SmartScreen 提示（口径见 docs\anti-virus.md）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\release.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Python .\.venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    # 打包用的解释器路径；留空则依次尝试 .packenv、.venv（与 build_exe.ps1 同一套解析）
    [string]$Python
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # 1. 解释器（照抄 build_exe.ps1 的顺序：优先干净的 .packenv，没有才退回 .venv）
    $packenvPython = Join-Path $root ".packenv\Scripts\python.exe"
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (-not $Python) {
        if (Test-Path $packenvPython) {
            $Python = $packenvPython
        }
        elseif (Test-Path $venvPython) {
            Write-Warning "找不到 .packenv，回退到 .venv（若里面有 Streamlit 时代的遗留依赖，产物体积会偏大）；建议先跑 scripts\setup_packenv.ps1"
            $Python = $venvPython
        }
        else {
            throw "找不到解释器：既没有 .packenv 也没有 .venv（先跑 scripts\setup_packenv.ps1）"
        }
    }
    if (-not (Test-Path $Python)) {
        throw "指定的解释器不存在：$Python"
    }
    $Python = (Resolve-Path $Python).Path

    # 2. 版本号：以 src\mla\__init__.py 为准，包名与产物名都由它派生
    $version = (& $Python -c "import mla; print(mla.__version__)" 2>&1 | Out-String).Trim()
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "读取版本号失败（退出码 $LASTEXITCODE）"
    }
    if (-not $version) {
        throw "读取版本号失败：输出为空"
    }
    Write-Host "[1/4] 版本号：$version（解释器：$Python）"

    # 3. 重建 exe。用子进程跑：build_exe.ps1 末尾的 `exit 0` 会把整个会话一起退掉
    Write-Host "[2/4] 调 scripts\build_exe.ps1 重建 dist\mla\"
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "build_exe.ps1") -Python $Python
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "build_exe.ps1 失败（退出码 $LASTEXITCODE）"
    }

    $distMla = Join-Path $root "dist\mla"
    $exe = Join-Path $distMla "mla.exe"
    if (-not (Test-Path $exe)) {
        throw "打包产物不存在：$exe"
    }

    # 4. 压缩。不用 Compress-Archive：PowerShell 5.1（.NET Framework）实现写出的条目名是
    #    `mla\mla.exe`（反斜杠），Zip 规范要求 `/`，Linux/macOS 解压会得到带反斜杠的文件名。
    #    这里手动按 ZipArchive 写入并统一用 `/`。
    $zipName = "mla-$version-windows-x64.zip"
    $zipPath = Join-Path $root "dist\$zipName"
    $shaPath = "$zipPath.sha256"
    foreach ($stale in @($zipPath, $shaPath)) {
        if (Test-Path $stale) {
            Remove-Item $stale -Force
        }
    }

    Write-Host "[3/4] 压缩 dist\mla -> dist\$zipName"
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $sourceDir = (Resolve-Path $distMla).Path.TrimEnd('\')
    $files = @(Get-ChildItem $sourceDir -Recurse -File)
    if ($files.Count -eq 0) {
        throw "源目录为空：$sourceDir"
    }

    $zipStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Create)
    $archive = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($sourceDir.Length + 1).Replace('\', '/')
            $entry = $archive.CreateEntry("mla/$relative", [System.IO.Compression.CompressionLevel]::Optimal)
            # 大文件（mla.exe 十几 MB）用流拷贝，别走内存字节数组
            $target = $entry.Open()
            $source = [System.IO.File]::OpenRead($file.FullName)
            try {
                $source.CopyTo($target)
            }
            finally {
                $source.Close()
                $target.Close()
            }
        }
    }
    finally {
        $archive.Dispose()
        $zipStream.Close()
    }

    # 结构自检：顶层目录只有 mla、无一处反斜杠、条目数与源文件数一致、含 mla.exe。
    # 压错层级或分隔符在解压端才会暴露，太晚。
    $reader = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entries = @($reader.Entries)
        $withBackslash = @($entries | Where-Object { $_.FullName.Contains('\') })
        $tops = @($entries | ForEach-Object { $_.FullName.Split('/')[0] } | Sort-Object -Unique)
        $hasExe = @($entries | Where-Object { $_.FullName -eq "mla/mla.exe" }).Count -gt 0
    }
    finally {
        $reader.Dispose()
    }
    if ($withBackslash.Count -ne 0) {
        throw "zip 条目名含反斜杠（$($withBackslash.Count) 个），首个：$($withBackslash[0].FullName)"
    }
    if ($tops.Count -ne 1 -or $tops[0] -ne "mla") {
        throw "zip 顶层目录应为单一的 mla，实际：$($tops -join ', ')"
    }
    if ($entries.Count -ne $files.Count) {
        throw "zip 条目数（$($entries.Count)）与源文件数（$($files.Count)）不一致"
    }
    if (-not $hasExe) {
        throw "zip 内找不到 mla/mla.exe"
    }

    # 5. SHA256：`<哈希>  <文件名>`，UTF-8 无 BOM + LF，跨平台校验工具都能直接读
    Write-Host "[4/4] 计算 SHA256"
    $hash = (Get-FileHash $zipPath -Algorithm SHA256).Hash
    $shaLine = "{0}  {1}`n" -f $hash, $zipName
    [System.IO.File]::WriteAllText($shaPath, $shaLine, (New-Object System.Text.UTF8Encoding($false)))

    $sizeMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
    Write-Host ""
    Write-Host ("zip：{0}" -f $zipPath)
    Write-Host ("体积：{0} MB" -f $sizeMb)
    Write-Host ("SHA256：{0}" -f $hash)
    Write-Host ("校验文件：{0}" -f $shaPath)
    # 显式退出码：前面跑过会往 stderr 写字的前端工具（vite / npm），
    # powershell -File 可能把「$? 为假」当成脚本失败，调用方会误判。
    exit 0
}
finally {
    Pop-Location
}
