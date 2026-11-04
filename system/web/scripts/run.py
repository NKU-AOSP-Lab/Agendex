#!/usr/bin/env python3
"""启动本地 Agendex；默认同一地址提供网页和 API，--dev 启用前端热更新。"""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time


WEB = Path(__file__).resolve().parents[1]
# The site lives under system/web; research data belongs to the workspace.
ROOT = WEB.parents[1]


def python_executable() -> str:
    candidates = [WEB / ".venv" / "bin" / "python", WEB / ".venv" / "Scripts" / "python.exe"]
    for path in candidates:
        if path.is_file():
            return str(path)
    return sys.executable


def dataset_ready(path: Path, require_keyword: bool = False) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(str(path)) as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='dataset'").fetchone()
            if row is None:
                return False
            dataset = json.loads(row[0])
            if require_keyword:
                if dataset.get("dataset_id") == "keyword-v2-prescreen":
                    # Default startup upgrades visible membership using local completed results.
                    return False
                if dataset.get("dataset_id") != "keyword-v2-fine-screen":
                    raise ValueError("默认工作库包含其他数据集，请明确使用--database查看历史库")
            return True
    except sqlite3.Error:
        return False


def main() -> int:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--dev", action="store_true", help="同时启动 Vite 开发服务器")
    command.add_argument("--build", action="store_true", help="启动前重新构建网页")
    command.add_argument("--port", type=int, default=8000, help="本地 API/网站端口，默认 8000")
    command.add_argument("--frontend-port", type=int, default=5173, help="开发模式网页端口，默认 5173")
    command.add_argument("--database", type=Path, help="可选的 SQLite 工作数据库")
    args = command.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.frontend_port <= 65535:
        command.error("端口必须在 1–65535 之间")
    python = python_executable()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(WEB / "backend") + os.pathsep + environment.get("PYTHONPATH", "")
    explicit_database = args.database is not None or bool(environment.get("AGENDEX_DB_PATH"))
    database = (args.database or Path(environment.get("AGENDEX_DB_PATH", str(WEB / "data" / "agendex-keyword.sqlite3")))).resolve()
    environment["AGENDEX_DB_PATH"] = str(database)
    api_url = "http://127.0.0.1:{}".format(args.port)
    environment["AGENDEX_API_TARGET"] = api_url
    environment["AGENDEX_LOCAL_PORTS"] = "{},{}".format(args.port, args.frontend_port)

    check = subprocess.run([python, "-c", "import fastapi,uvicorn"], capture_output=True)
    if check.returncode:
        print("请先创建 system/web/.venv，并安装 system/web/backend/requirements.lock。", file=sys.stderr)
        return 1
    try:
        ready = dataset_ready(database, require_keyword=not explicit_database)
    except (ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if not ready:
        print("首次启动：导入已筛选的仓库元数据。", flush=True)
        imported = subprocess.run([python, str(WEB / "scripts" / "import_dataset.py"), "--database", str(database)],
                                  cwd=str(ROOT), env=environment)
        if imported.returncode:
            return imported.returncode
    npm = shutil.which("npm")
    if args.dev and not (WEB / "node_modules" / "vite").exists():
        print("请先在 web 目录运行 npm ci。", file=sys.stderr)
        return 1
    if not args.dev and (args.build or not (WEB / "dist" / "index.html").exists()):
        if not npm:
            print("构建网页需要 Node.js/npm，请先安装。", file=sys.stderr)
            return 1
        built = subprocess.run([npm, "run", "build"], cwd=str(WEB), env=environment)
        if built.returncode:
            return built.returncode

    children = []

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        children.append(subprocess.Popen(
            [python, "-m", "uvicorn", "agendex.app:app", "--host", "127.0.0.1", "--port", str(args.port),
             "--log-level", "warning", "--no-access-log"], cwd=str(WEB / "backend"), env=environment))
        if args.dev:
            if not npm:
                print("开发模式需要 Node.js/npm。", file=sys.stderr)
                return 1
            children.append(subprocess.Popen([npm, "run", "dev", "--", "--port", str(args.frontend_port)],
                                             cwd=str(WEB), env=environment))
        address = "http://127.0.0.1:{}".format(args.frontend_port) if args.dev else api_url
        print("Agendex：{}\n按 Ctrl+C 停止。".format(address), flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.3)
        return next((child.returncode for child in children if child.returncode), 0)
    except KeyboardInterrupt:
        return 0
    finally:
        # 仅停止本次启动的子进程，不操作其他终端中的服务。
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
