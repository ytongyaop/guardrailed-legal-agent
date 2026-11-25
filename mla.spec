# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir + 控制台，入口 `src/mla/desktop.py`。

构建入口是 `scripts/build_exe.ps1`：它负责「装前端依赖 → 构建前端 → 同步到
`src/mla/api/static/`」，本文件只负责「收集 Python 侧的东西并打包」。直接跑
`python -m PyInstaller mla.spec` 也能用，但静态目录缺失会当场报错退出——
这比打出一个启动后白屏的 exe 要好。

刻意不做的几件事：

- **不用 onefile**：onefile 每次启动都要把整个目录解压到临时目录，首次启动一两秒起步，
  而且自解压行为本身就是杀软误报的高发点。onedir（COLLECT）只解压一次。
- **不用 UPX**（`upx=False`）：UPX 压缩过的二进制是误报重灾区，本项目体积可控，
  不值得为几 MB 冒这个险。
- **不收集运行期产物**：`data/index/`、`evalsets/last_*.json`、`evalsets/*_history.jsonl`
  是评测与向量缓存，可再生，与 `pyproject.toml` 的
  `[tool.setuptools.exclude-package-data]` 保持同一口径。
- **不改数据目录结构**：`mla/config.py` 用 `Path(__file__).parent / "data"` 定位数据，
  `mla/api/app.py` 用 `Path(__file__).parent / "static"` 定位前端，所以产物必须落在
  `_internal/mla/data` 与 `_internal/mla/api/static`，与源码树一一对应（PyInstaller 冻结后
  `__file__` 指向 `_internal` 里的真实文件，这条链路才成立）。
- **不收集 Streamlit 时代的遗留依赖**（实测结论）：`import mla.graph.build`、
  `mla.services.retrieval`、`mla.services.report` 都不会加载 pandas / pyarrow / PIL；
  只有 `mla.services.report` 会加载 **lxml**（python-docx 导出 Word 要用），所以 lxml 必须留，
  其余列进 `Analysis(excludes=...)` 兜底。打包环境本身也要干净（见 `scripts/setup_packenv.ps1`）。
"""

import fnmatch
import re
import sys
from pathlib import Path

#: spec 文件所在目录即仓库根（PyInstaller 注入的全局变量）。
ROOT = Path(SPECPATH).resolve()
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from mla import __version__ as MLA_VERSION  # noqa: E402  （只读 __init__，无副作用）

APP_NAME = "mla"
#: 版本资源里的产品名 / 公司名：填上它们不只是好看，也是杀软启发式评分的缓解项。
PRODUCT_NAME = "Guardrailed Legal Agent (MLA)"
COMPANY_NAME = "guardrailed-legal-agent"

DATA_DIR = SRC / "mla" / "data"
STATIC_DIR = SRC / "mla" / "api" / "static"

#: 与 pyproject `[tool.setuptools.exclude-package-data]` 一致的排除口径（相对 data/ 的 POSIX 路径）。
EXCLUDED_DATA_PATTERNS = (
    "index/*",
    "evalsets/last_*.json",
    "evalsets/*_history.jsonl",
)

#: uvicorn 的 loop / http / websocket 实现是**运行期按名字动态 import** 的
#: （`uvicorn/protocols/http/auto.py` 用 `importlib.import_module`），静态分析抓不到；
#: 漏了的表现是 exe 启动即 ImportError，而不是构建失败。
#: 其余条目按 `build/mla/warn-mla.txt` 的 missing module 报告补齐（见下方 HIDDEN_IMPORTS 注释）。
HIDDEN_IMPORTS = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "pydantic",
    "pydantic_core",
    "pydantic.deprecated.decorator",
]


def collect_tree(source: Path, dest: str, *, excluded: tuple[str, ...] = ()) -> list:
    """把目录树摊成 PyInstaller 的 `(源文件, 目标目录)` 列表，顺带剔除运行期产物。

    逐个文件枚举而不是用 `Tree()`：排除规则只对其中一部分文件生效，`Tree` 的 excludes
    走的是语义路径匹配，跨平台分隔符容易踩坑；显式枚举虽然啰嗦，但「打包了什么」一眼可查。
    """
    entries: list = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        posix = relative.as_posix()
        if any(fnmatch.fnmatch(posix, pattern) for pattern in excluded):
            continue
        parent = relative.parent
        target = dest if str(parent) == "." else str(Path(dest) / parent)
        entries.append((str(path), target))
    return entries


if not (STATIC_DIR / "index.html").is_file():
    raise SystemExit(
        f"缺少前端构建产物：{STATIC_DIR / 'index.html'}\n"
        "先跑 scripts/build_exe.ps1，或手动 `npm --prefix web run build` "
        "再把 web/dist 的内容复制到 src/mla/api/static/。"
    )

datas = collect_tree(DATA_DIR, "mla/data", excluded=EXCLUDED_DATA_PATTERNS)
datas += collect_tree(STATIC_DIR, "mla/api/static")


def _version_tuple(version: str) -> tuple:
    """`0.4.0` / `1.0.0-dev` 这类版本串 → Windows 版本资源要的四段整数。"""
    numbers = [int(part) for part in re.findall(r"\d+", version)[:4]]
    return tuple((numbers + [0, 0, 0, 0])[:4])


def write_version_file(version: str) -> Path:
    """生成 Windows 版本资源文件（`EXE(version=...)` 要的就是这个文本格式）。

    这不是装饰：没有版本资源的 exe 在「属性 → 详细信息」里一片空白，
    也属于杀软启发式评分的常见扣分项。语言 0x0804 = 简体中文，代码页 1200 = Unicode。
    """
    target = ROOT / "build" / "mla-version-info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    four = _version_tuple(version)
    target.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={four},
    prodvers={four},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [
          StringStruct('CompanyName', '{COMPANY_NAME}'),
          StringStruct('FileDescription', '{PRODUCT_NAME}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('InternalName', '{APP_NAME}'),
          StringStruct('LegalCopyright', 'MIT License'),
          StringStruct('OriginalFilename', '{APP_NAME}.exe'),
          StringStruct('ProductName', '{PRODUCT_NAME}'),
          StringStruct('ProductVersion', '{version}'),
        ],
      )
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ],
)
""",
        encoding="utf-8",
    )
    return target


a = Analysis(
    [str(SRC / "mla" / "desktop.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter 只有一个用途是「给本地脚本弹窗」，本项目全程无 GUI 依赖，直接剔除省体积。
    # 下面这些是 Streamlit 时代的遗留依赖与可选集成，运行时不需要（见模块 docstring 的实测）：
    # 干净环境里本来就不会装；列在这里是防御——万一在脏环境构建，也不会把上百 MB 收进包。
    # 注意 lxml 不能加：python-docx 导出 Word 报告要用它。
    excludes=[
        "tkinter",
        "streamlit",
        "altair",
        "pydeck",
        "narwhals",
        "pandas",
        "pyarrow",
        "PIL",
        "matplotlib",
        "IPython",
        "notebook",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(write_version_file(MLA_VERSION)),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
