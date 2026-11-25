#Requires -Version 5.1
<#
.SYNOPSIS
    把「FastAPI 后端 + Vue 前端」打成 Windows 桌面 exe（1.0.0 打包批次一）。

.DESCRIPTION
    四步流水线：
      1. `npm --prefix web ci`——只在 web\node_modules 不存在时执行（ci 会先删整个目录，几分钟起步）；
      2. `npm --prefix web run build`——产出 web\dist；
      3. 把 web\dist\* 同步到 src\mla\api\static\（**先清空目标目录**）；
      4. `<python> -m PyInstaller mla.spec --noconfirm --clean`——产出 dist\mla\mla.exe。

    第 3 步的清空不是洁癖：Vite 每次构建给 assets 里的文件名加内容哈希，
    不清空会残留上一版的哈希文件，打进 exe 既白占体积，也可能让 SPA 命中旧资源。

    解释器（-Python）决定 PyInstaller 收集哪些包：默认用 `.packenv`（scripts\setup_packenv.ps1
    建的干净环境），没有才退回 `.venv`。脏环境里构建会把 Streamlit 时代的遗留依赖一并收进去。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_packenv.ps1
    powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1 -Python .\.venv\Scripts\python.exe
#>
[CmdletBinding()]
param(
    # 打包用的解释器路径；留空则依次尝试 .packenv、.venv
    [string]$Python
)

$ErrorActionPreference = "Stop"

function Assert-LastExitCode {
    param([string]$Step)
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "$Step 失败（退出码 $LASTEXITCODE）"
    }
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
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
    $pythonVersion = (& $Python -V 2>&1 | Out-String).Trim()
    Write-Host "[0/4] 打包解释器：$Python"
    Write-Host "      版本：$pythonVersion"
    if ($Python -ne $packenvPython) {
        Write-Warning "当前不是 .packenv 环境，PyInstaller 会按这个解释器收集包"
    }

    $webDir = Join-Path $root "web"
    $distDir = Join-Path $webDir "dist"
    $staticDir = Join-Path $root "src\mla\api\static"

    # 1. 前端依赖
    if (-not (Test-Path (Join-Path $webDir "node_modules"))) {
        Write-Host "[1/4] npm ci：安装前端依赖"
        npm --prefix web ci
        Assert-LastExitCode "npm ci"
    }
    else {
        Write-Host "[1/4] 跳过 npm ci：web\node_modules 已存在"
    }

    # 2. 构建前端
    Write-Host "[2/4] npm run build：构建前端"
    npm --prefix web run build
    Assert-LastExitCode "npm run build"

    # 3. 同步前端产物到包内静态目录（先清空再复制）
    if (-not (Test-Path (Join-Path $distDir "index.html"))) {
        throw "前端构建产物缺失：$distDir\index.html"
    }
    Write-Host "[3/4] 同步前端产物：web\dist -> src\mla\api\static"
    if (Test-Path $staticDir) {
        Remove-Item $staticDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $staticDir -Force | Out-Null
    Copy-Item -Path (Join-Path $distDir "*") -Destination $staticDir -Recurse -Force

    # 4. 打包
    Write-Host "[4/4] PyInstaller：打包 dist\mla\mla.exe"
    & $python -m PyInstaller mla.spec --noconfirm --clean
    Assert-LastExitCode "PyInstaller"

    $exe = Join-Path $root "dist\mla\mla.exe"
    if (-not (Test-Path $exe)) {
        throw "打包流程结束但产物不存在：$exe"
    }
    $total = (Get-ChildItem (Join-Path $root "dist\mla") -Recurse -File | Measure-Object Length -Sum).Sum

    Write-Host ""
    Write-Host ("产物：{0}" -f $exe)
    Write-Host ("单文件：{0:N1} MB" -f ((Get-Item $exe).Length / 1MB))
    Write-Host ("整个目录：{0:N1} MB" -f ($total / 1MB))
    # 显式退出码：前面跑过会往 stderr 写字的前端工具（vite / npm），
    # powershell -File 可能把「$? 为假」当成脚本失败，CI 会误判。
    exit 0
}
finally {
    Pop-Location
}
