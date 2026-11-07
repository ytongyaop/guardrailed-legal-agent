"""清理演示/测试账号及其企业、合同、任务、日志（默认只预览，加 --yes 才删除）。

用法：
    python scripts/cleanup_test_data.py                # 预览
    python scripts/cleanup_test_data.py --yes          # 执行
    python scripts/cleanup_test_data.py --prefix test  # 自定义前缀
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DB = Path("data/mla.sqlite3")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理测试账号（默认预览）")
    parser.add_argument("--prefix", action="append", default=None, help="用户名前缀，可多次")
    parser.add_argument("--yes", action="store_true", help="真正执行删除")
    args = parser.parse_args(argv)
    prefixes = args.prefix or ["iso", "isolation"]

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    users = [
        row
        for row in conn.execute("SELECT user_id, username, enterprise_id FROM users")
        if any(str(row["username"]).startswith(p) for p in prefixes)
    ]
    if not users:
        print("没有匹配的测试账号。")
        return 0

    print(f"匹配 {len(users)} 个账号：")
    for user in users:
        ent = user["enterprise_id"] or ""
        tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE enterprise_id=?", (ent,)).fetchone()[0]
        contracts = conn.execute("SELECT COUNT(*) FROM contracts WHERE enterprise_id=?", (ent,)).fetchone()[0]
        logs = conn.execute("SELECT COUNT(*) FROM logs WHERE user_id=?", (user["user_id"],)).fetchone()[0]
        print(f"  {user['username']}  企业={ent or '无'}  任务={tasks}  合同={contracts}  日志={logs}")

    if not args.yes:
        print("\n预览模式：未删除。确认后加 --yes 执行。")
        return 0

    for user in users:
        ent = user["enterprise_id"] or ""
        conn.execute("DELETE FROM logs WHERE user_id=?", (user["user_id"],))
        if ent:
            conn.execute("DELETE FROM tasks WHERE enterprise_id=?", (ent,))
            conn.execute("DELETE FROM contracts WHERE enterprise_id=?", (ent,))
            conn.execute("DELETE FROM enterprises WHERE enterprise_id=?", (ent,))
        conn.execute("DELETE FROM users WHERE user_id=?", (user["user_id"],))
    conn.commit()
    print(f"已删除 {len(users)} 个测试账号及其数据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
