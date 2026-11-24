"""桌面启动入口：把 API + 前端打进 exe 后，双击即用（1.0.0 打包批次一）。

与 `cli.py` 的分工：CLI 是给开发者与排障用的多命令入口（argparse 会吃掉 exe 自己的参数，
`mla.exe check ...` 这种语义在这里也不成立），所以**不复用** `cli.main()`。这里只做三件事：
选端口 → 起 uvicorn → 延迟开浏览器。

三个必须记住的打包约束（都来自实际踩坑，别「优化」掉）：

1. `multiprocessing.freeze_support()` 必须在**任何其他 import 之前**调用。冻结成 exe 后，
   `multiprocessing` 的任何用法都会重新执行入口脚本；不先做分流，就会出现无限自拉起。
2. `uvicorn.run()` 必须传 **app 对象**，不能传 `"mla.api.app:create_app"` 这种 import string：
   frozen 下 uvicorn 的多进程 / reload 路径靠重新 import 模块工作，字符串形式会把 exe 自己
   当成 Python 解释器再拉一遍。
3. `loop="asyncio"` 显式指定：Windows 没有 uvloop，交给 auto 探测只是多一层不确定。
   端口也不写死 8000：本机 8000 可能已被别的服务占用，绑定 0 让系统分配空闲端口。

控制台会先打印一行 `MLA_PORT=<port>` 再打印给人看的地址：浏览器没弹出来时，用户至少能从
控制台复制地址；自动化脚本（`scripts/build_exe.ps1` 的冒烟测试）也靠这行取端口，
不必去猜随机端口。
"""

from __future__ import annotations

import multiprocessing

# 必须在最前面：frozen 下子进程会重新执行本模块，先 freeze_support() 才能正确分流。
multiprocessing.freeze_support()

import socket  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import webbrowser  # noqa: E402

#: 只绑回环地址，与 `mla.api.app.DEFAULT_HOST` 同一口径（本机以外不开放）。
HOST = "127.0.0.1"

#: 机器可读的端口行前缀：`MLA_PORT=54321`（控制台、冒烟脚本共用）。
PORT_LINE_PREFIX = "MLA_PORT="

#: 延迟开浏览器：给 uvicorn 留出完成绑定与启动的时间，太早打开会看到连接被拒。
BROWSER_DELAY_S = 1.2


def pick_free_port() -> int:
    """向系统要一个空闲端口：绑定 0、读回实际端口、立刻释放。

    绑定与释放之间窗口极小（uvicorn 紧接着就 bind），换来的是「不去跟本机别的服务抢 8000」。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _open_browser(url: str) -> None:
    """后台线程里开浏览器：失败只给一行警告，绝不影响服务本身。"""
    try:
        opened = webbrowser.open(url)
    except OSError:  # 没有默认浏览器 / 系统 API 受限
        opened = False
    if not opened:
        print(f"未能自动打开浏览器，请手动访问 {url}", file=sys.stderr, flush=True)


def main() -> int:
    from mla.api.app import create_app

    port = pick_free_port()
    url = f"http://{HOST}:{port}"
    print(f"{PORT_LINE_PREFIX}{port}", flush=True)
    print(f"服务已启动，地址是 {url}", flush=True)
    print("关闭本窗口或按 Ctrl+C 退出。", flush=True)

    timer = threading.Timer(BROWSER_DELAY_S, _open_browser, args=(url,))
    timer.daemon = True
    timer.start()
    try:
        import uvicorn

        uvicorn.run(
            create_app(),  # 传对象而非 import string，见模块 docstring 第 2 条
            host=HOST,
            port=port,
            workers=1,
            reload=False,
            loop="asyncio",
        )
    except KeyboardInterrupt:  # Ctrl+C
        print("已退出。", flush=True)
    finally:
        timer.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(main())
