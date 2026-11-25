#Requires -Version 5.1
<#
.SYNOPSIS
    建一个只含「运行 + 打包」依赖的干净环境 `.packenv`（1.0.0 可交付包）。

.DESCRIPTION
    为什么需要单独的打包环境：本机 `.venv` 从 Streamlit 时代一路升级过来，残留了
    streamlit / pandas / pyarrow / numpy / pillow 等上百 MB 的包。PyInstaller 是按
    **当前解释器里装了什么** 来收集依赖的，在脏环境里构建，产物会从 ~60 MB 膨胀到 ~200 MB，
    而且收进去的都是运行时用不到的东西。

    做的四件事：
      1. `uv venv --python 3.12 .packenv`（3.12 与 CI 测试版本一致）；
      2. `uv pip install -e ".[files,web,package]"`（运行 + 打包，不含 dev / 不含 Streamlit）；
      3. 打印安装清单，供人工核对；
      4. 断言环境干净：`import streamlit` 必须失败。

    幂等：`.packenv` 已存在且能 `import mla` 就跳过创建与安装；要重建加 `-Force`。

    回退：首选 3.12 装不上（本机曾出现网络超时）就试 3.13；再失败才退回 `.venv`，
    并在输出里写明用了哪个、为什么——绝不静默降级。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_packenv.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_packenv.ps1 -Force
#>
[CmdletBinding()]
param(
    # 首选 Python 版本，与 CI 测试矩阵里的 3.12 对齐
    [string]$Python = "3.12",
    # 首选版本装不上时的回退版本
    [string]$FallbackPython = "3.13",
    # 单次依赖安装的硬超时（秒）。本机曾出现「连上 PyPI 但传输停滞」，
    # uv 自己的 UV_HTTP_TIMEOUT 对「连接活着但极慢」不生效，所以外面再加一层。
    [int]$InstallTimeoutSec = 600,
    # 忽略已有 .packenv，强制重建
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $packenv = Join-Path $root ".packenv"
    $packenvPython = Join-Path $packenv "Scripts\python.exe"
    $requirements = ".[files,web,package]"

    function Test-PackenvReady {
        if (-not (Test-Path $packenvPython)) {
            return $false
        }
        return (Test-PythonCommand @("-c", "import mla"))
    }

    # PS 5.1 的坑：EAP=Stop 时，原生命令往 stderr 写东西会被当成终止错误（NativeCommandError）。
    # 探针（import mla / import streamlit）本来就预期可能失败，临时把 EAP 降回 Continue。
    function Test-PythonCommand {
        param([string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            & $packenvPython @Arguments *> $null
            return ($LASTEXITCODE -eq 0)
        }
        finally {
            $ErrorActionPreference = $previous
        }
    }

    function Remove-Packenv {
        if (Test-Path $packenv) {
            Write-Host "  清理半成品：$packenv"
            Remove-Item $packenv -Recurse -Force
        }
    }

    # uv 在本机偶发「连上 PyPI 但传输停滞」：给每次调用一个硬超时，超时就杀进程。
    # 判定成败不看退出码：PS 5.1 里 Start-Process -PassThru 拿到的对象 ExitCode 连 WaitForExit() 之后也是空，
    # 所以用「结果是否达成」代替（venv 里有 python.exe / 能 import mla）。
    function Invoke-UvUntil {
        param(
            [string[]]$Arguments,
            [int]$TimeoutSec,
            [scriptblock]$Succeeded
        )
        $uvExe = (Get-Command uv -ErrorAction Stop).Source
        $proc = Start-Process -FilePath $uvExe -ArgumentList $Arguments -NoNewWindow -PassThru
        if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
            Write-Warning ("uv {0} 超过 {1} 秒未完成，终止" -f ($Arguments -join " "), $TimeoutSec)
            try { $proc.Kill() } catch { }
            $proc.WaitForExit(5000) | Out-Null
            return $false
        }
        return (& $Succeeded)
    }

    function New-Packenv {
        param([string]$Version)

        Write-Host "  尝试 Python $Version ..."
        Remove-Packenv
        $venvArgs = @("venv", "--python", $Version, ".packenv")
        if (-not (Invoke-UvUntil -Arguments $venvArgs -TimeoutSec 300 -Succeeded {
                    Test-Path $packenvPython
                })) {
            Write-Warning "uv venv --python $Version 失败或超时"
            return $false
        }

        # 停滞多半是某个 wheel 的下载连接卡住；已下完的部分留在 uv 缓存里，重试不用从头来
        $installArgs = @("pip", "install", "--python", $packenvPython, "-e", $requirements)
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            Write-Host "  安装依赖（第 $attempt 次，超时 $InstallTimeoutSec 秒）..."
            if (Invoke-UvUntil -Arguments $installArgs -TimeoutSec $InstallTimeoutSec -Succeeded {
                        Test-PythonCommand @("-c", "import mla")
                    }) {
                return $true
            }
            Write-Warning "在 Python $Version 上安装依赖失败或超时（第 $attempt 次）"
        }
        Remove-Packenv
        return $false
    }

    # 取版本号也要护住：`2>&1` 在有 stderr 时同样会触发 NativeCommandError
    function Get-PythonVersion {
        param([string]$Exe)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            return (& $Exe -V 2>&1 | Out-String).Trim()
        }
        finally {
            $ErrorActionPreference = $previous
        }
    }

    $usedVersion = $null
    $usedReason = $null

    if ((-not $Force) -and (Test-PackenvReady)) {
        $usedVersion = Get-PythonVersion $packenvPython
        $usedReason = "已存在且能 import mla，跳过创建与安装（要重建加 -Force）"
    }
    else {
        Write-Host "创建干净打包环境：$packenv"
        $env:UV_HTTP_TIMEOUT = "60"
        foreach ($candidate in @($Python, $FallbackPython)) {
            if (New-Packenv -Version $candidate) {
                $usedVersion = Get-PythonVersion $packenvPython
                $usedReason = "Python $candidate 创建并安装成功"
                break
            }
        }

        if (-not $usedVersion) {
            # 最后兜底：现成的 .venv。能打，但里面可能有 Streamlit 时代的遗留包，体积会偏大。
            $venvPython = Join-Path $root ".venv\Scripts\python.exe"
            if (-not (Test-Path $venvPython)) {
                throw "Python $Python / $FallbackPython 都装不上，且 .venv 不存在。检查网络后重试，或先手工建 .venv"
            }
            Write-Warning "Python $Python / $FallbackPython 均安装失败，回退到 .venv（可能有遗留依赖，产物体积偏大）"
            $packenvPython = $venvPython
            $usedVersion = Get-PythonVersion $venvPython
            $usedReason = ".packenv 建不起来，回退到 .venv"
        }
    }

    Write-Host ""
    Write-Host "解释器：$packenvPython"
    Write-Host "版本：  $usedVersion"
    Write-Host "原因：  $usedReason"

    Write-Host ""
    Write-Host "已安装的包（核对：不应出现 streamlit / pandas / pyarrow / altair / pydeck / narwhals）："
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & uv pip list --python $packenvPython
        $listExit = $LASTEXITCODE
        $checkScript = @'
import importlib.util
import sys

names = ["streamlit", "pandas", "pyarrow", "altair", "pydeck", "narwhals"]
found = [n for n in names if importlib.util.find_spec(n) is not None]
if found:
    print("dirty: " + ", ".join(found))
    sys.exit(1)
print("clean: none of " + ", ".join(names))
'@
        $checkScript | & $packenvPython -
        $checkExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($listExit -ne 0) {
        throw "uv pip list 失败（退出码 $listExit）"
    }
    if ($checkExit -ne 0) {
        throw "打包环境不干净：仍有 Streamlit 时代的遗留依赖（加 -Force 重建）"
    }

    Write-Host ""
    # 独立断言一次 `import streamlit`：这是「环境干净」最直观的证词，也是验收要贴的证据。
    if (Test-PythonCommand @("-c", "import streamlit")) {
        throw "断言失败：打包环境里居然能 import streamlit（本应失败）"
    }
    Write-Host "断言通过：import streamlit 失败（ModuleNotFoundError）"

    Write-Host ""
    Write-Host "下一步：powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1"
    # 显式退出码：脚本里跑过预期失败的原生命令（import streamlit 探针），
    # 不写这一行时 powershell -File 可能把 $LASTEXITCODE 的 1 当成脚本失败，CI 会误判。
    exit 0
}
finally {
    Pop-Location
}
