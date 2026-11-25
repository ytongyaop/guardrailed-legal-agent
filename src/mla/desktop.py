"""桌面启动入口：把 API + 前端打进 exe 后，双击即用（1.0.0 打包批次一）。

与 `cli.py` 的分工：CLI 是给开发者与排障用的多命令入口（argparse 会吃掉 exe 自己的参数，
`mla.exe check ...` 这种语义在这里也不成立），所以**不复用** `cli.main()`。这里只做四件事：
选端口 → 起 uvicorn → 开浏览器 → 落日志。

**控制台是有意保留的（`mla.spec` 里 `console=True`，别改成 False）**：

- 用户能看到启动地址和错误信息，出问题时不用去猜「双击了但没反应」；
- 「关闭控制台窗口 = 停止服务」语义直观，不需要额外的托盘程序或退出菜单；
- 省掉 `sys.stdout is None` 的一堆兜底分支——windowed 模式下 stdout 为 None，
  每一处 print 都要判空，代码会变脏而收益只是「看不见一个黑窗口」。

几个必须记住的打包约束（都来自实际踩坑，别「优化」掉）：

1. `multiprocessing.freeze_support()` 必须在**任何其他 import 之前**调用。冻结成 exe 后，
   `multiprocessing` 的任何用法都会重新执行入口脚本；不先做分流，就会出现无限自拉起。
2. uvicorn 必须拿到 **app 对象**，不能传 `"mla.api.app:create_app"` 这种 import string：
   frozen 下 uvicorn 的多进程 / reload 路径靠重新 import 模块工作，字符串形式会把 exe 自己
   当成 Python 解释器再拉一遍。
3. `loop="asyncio"` 显式指定：Windows 没有 uvloop，交给 auto 探测只是多一层不确定。
4. 端口不写死 8000：本机 8000 可能已被别的服务占用。这里改成「先 bind 一个 socket，
   再把同一个 socket 交给 uvicorn」——绑定与监听之间没有「先释放再抢」的窗口，
   也就不会被别的进程抢走。

环境变量（自动化与 CI 用，人不需要设）：

- `MLA_NO_BROWSER=1`：不自动开浏览器。
- `MLA_PORT_FILE=<绝对路径>`：把端口写进该文件（含换行），便于脚本等待与读取。
- `MLA_HOME` / `MLA_DB` 照旧，由 `mla.config` / `mla.services.bootstrap` 解析。

控制台会先打印一行机器可读的 `MLA_PORT=<port>` 再打印给人看的地址：浏览器没弹出来时，
用户至少能从控制台复制地址；自动化脚本靠这行（或 `MLA_PORT_FILE`）取端口。

日志写在 `<MLA_HOME>/logs/mla.log`（追加），只记启动、端口、异常与耗时，**不记任何 Key**。
"""

from __future__ import annotations

import multiprocessing

# 必须在最前面：frozen 下子进程会重新执行本模块，先 freeze_support() 才能正确分流。
multiprocessing.freeze_support()

import ctypes  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

#: 只绑回环地址（本机以外不开放），与 `mla.api.app.DEFAULT_HOST` 同一口径。
HOST = "127.0.0.1"

#: 机器可读的端口行前缀：`MLA_PORT=54321`（控制台、冒烟脚本共用）。
PORT_LINE_PREFIX = "MLA_PORT="

#: 延迟开浏览器：给 uvicorn 留出完成启动的时间，太早打开会看到连接被拒。
BROWSER_DELAY_S = 1.2

#: 控制台标题：把「关窗口 = 停服务」写在标题栏上，比只打一行提示更难被忽略。
CONSOLE_TITLE = "用工风险核对助手 - 关闭此窗口即退出"

LOGGER_NAME = "mla.desktop"


def _set_console_title(title: str = CONSOLE_TITLE) -> None:
    """设置控制台窗口标题；非 Windows 或调用失败都静默跳过，绝不因此启动失败。"""
    if os.name != "nt":
        return
    windll = getattr(ctypes, "windll", None)  # 非 Windows 上不存在
    if windll is None:
        return
    try:
        windll.kernel32.SetConsoleTitleW(title)
    except Exception:  # noqa: BLE001  （标题只是锦上添花，任何失败都不值得中断启动）
        pass


def _setup_logging() -> tuple[logging.Logger, Path | None]:
    """给本模块挂一个文件 handler：`<MLA_HOME>/logs/mla.log`，追加写。

    返回 `(logger, 日志路径)`；日志目录不可写时返回 `(logger, None)` 并继续跑——
    日志是排障辅助，不能成为启动的前置条件。
    """
    from mla.config import ConfigError, mla_home

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 同一进程里重复调用（测试、二次启动）时不叠加 handler
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    try:
        log_dir = mla_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "mla.log"
        file_handler = logging.FileHandler(path, mode="a", encoding="utf-8")
    except (OSError, ConfigError):
        logger.addHandler(logging.NullHandler())
        return logger, None

    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [pid=%(process)d] %(message)s"))
    logger.addHandler(file_handler)
    return logger, path


def _make_console_safe() -> None:
    """控制台编码兜底：非中文 Windows 的 ANSI 代码页编不了中文，print 会抛 UnicodeEncodeError。

    只把错误策略改成 replace（少数乱码字符，但不会崩），**不改编码本身**：
    交互式控制台在 Windows 上走 `WriteConsoleW`，本来就是宽字符，改编码只会引入新变量。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # 理论上 console=True 不会出现，兜一下不花钱
            continue
        # 用 getattr 取：typeshed 把 sys.stdout 声明成 TextIO，动态替换过的流不一定是 TextIOWrapper
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


def _bind_socket() -> tuple[socket.socket, int]:
    """绑定回环地址的 0 端口，返回**仍然处于绑定状态**的 socket 与实际端口。

    与旧做法（bind → 读端口 → 关闭 → 让 uvicorn 重新 bind）的区别：这里 socket 不释放，
    直接交给 `uvicorn.Server.run(sockets=[sock])`，中间没有可被别的进程抢占的窗口。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, 0))
    except OSError:
        sock.close()
        raise
    return sock, int(sock.getsockname()[1])


def _open_browser(url: str) -> None:
    """后台线程里开浏览器：失败只给一行警告，绝不影响服务本身。"""
    try:
        opened = webbrowser.open(url)
    except OSError:  # 没有默认浏览器 / 系统 API 受限
        opened = False
    if not opened:
        print(f"未能自动打开浏览器，请手动复制地址到浏览器：{url}", file=sys.stderr, flush=True)


def _browser_disabled() -> bool:
    """`MLA_NO_BROWSER=1`（CI / 自动化）时不开浏览器。"""
    return (os.environ.get("MLA_NO_BROWSER") or "").strip().lower() in {"1", "true", "yes"}


def _write_port_file(port: int) -> None:
    """`MLA_PORT_FILE` 设了就写端口：自动化不用去解析控制台输出。失败只警告。"""
    raw = (os.environ.get("MLA_PORT_FILE") or "").strip()
    if not raw:
        return
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{port}\n", encoding="utf-8")
    except OSError as exc:
        print(f"未能写入端口文件 {path}：{exc}", file=sys.stderr, flush=True)


def _print_banner(url: str) -> None:
    """三段给人看的输出：地址 / 手动打开的办法 / 怎么退出。"""
    print(f"服务已启动，地址是 {url}", flush=True)
    print("浏览器没有自动打开的话，请手动复制上面的地址到浏览器。", flush=True)
    print("关闭本窗口即停止服务（也可以按 Ctrl+C）。", flush=True)


def main() -> int:
    _make_console_safe()
    _set_console_title()
    logger, log_path = _setup_logging()

    from mla import __version__
    from mla.api.app import create_app

    sock, port = _bind_socket()
    url = f"http://{HOST}:{port}"

    print(f"{PORT_LINE_PREFIX}{port}", flush=True)
    _print_banner(url)
    if log_path is not None:
        print(f"日志：{log_path}", flush=True)

    logger.info("启动 mla %s，地址 %s，pid=%d", __version__, url, os.getpid())
    _write_port_file(port)

    timer: threading.Timer | None = None
    if _browser_disabled():
        logger.info("MLA_NO_BROWSER 已设置，不自动打开浏览器")
    else:
        timer = threading.Timer(BROWSER_DELAY_S, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()

    try:
        import uvicorn

        config = uvicorn.Config(
            create_app(),  # 传对象而非 import string，见模块 docstring 第 2 条
            host=HOST,
            port=port,
            workers=1,
            reload=False,
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        # 同步入口：内部自建事件循环；socket 已在上面绑定，无需再 bind
        server.run(sockets=[sock])
        logger.info("服务正常退出")
    except KeyboardInterrupt:  # Ctrl+C
        print("已退出。", flush=True)
        logger.info("收到 Ctrl+C，服务退出")
    except BaseException:
        logger.exception("服务异常退出")
        where = f"，详情见日志：{log_path}" if log_path is not None else ""
        print(f"服务异常退出{where}", file=sys.stderr, flush=True)
        return 1
    finally:
        if timer is not None:
            timer.cancel()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
