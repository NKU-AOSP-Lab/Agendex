"""隔离的网页交互预览：临时库、合成数据、假模型，绝不调用 OpenRouter。

用于手动验证策略结果、阈值与历史恢复。只监听 8001 等测试端口，
临时数据随进程退出清理，不写入用户的网站数据库。
"""

import argparse
import os
from pathlib import Path
import tempfile


def preview_app(port: int):
    temporary = tempfile.TemporaryDirectory(prefix="agendex-ui-qa-")
    target = Path(temporary.name) / "preview.sqlite3"
    # 在导入 app 前确定隔离路径，连模块级默认 app 也不会碰用户数据库。
    os.environ["AGENDEX_DB_PATH"] = str(target)
    os.environ["AGENDEX_LOCAL_PORTS"] = str(port)

    from fastapi.testclient import TestClient
    from agendex.app import create_app
    from test_support import FakeJev, create_fixture, policy_body

    create_fixture(target, count=3)
    fake = FakeJev()
    setup_app = create_app(target, fake, start_workers=False)
    with TestClient(setup_app) as client:
        policy_response = client.post("/api/policies", json=policy_body(name="阈值演示（测试数据）"))
        if policy_response.status_code != 201:
            raise RuntimeError(policy_response.text)
        policy = policy_response.json()
        created = client.post("/api/jobs", json={
            "policy_id": policy["id"], "scope": {"repository_ids": [1, 2, 3]}, "skip_completed": True,
        })
        if created.status_code != 201:
            raise RuntimeError(created.text)
        while setup_app.state.engine.run_once():
            pass
    app = create_app(target, fake, start_workers=True)
    app.state.preview_directory = temporary

    @app.get("/__qa/requests")
    def requests():
        return {"fixture_requests": len(fake.requests), "remote_requests": 0}

    # create_app 最后的静态页面挂载会匹配所有路径，将测试路由放在它之前。
    app.router.routes.insert(0, app.router.routes.pop())

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 8000:
        parser.error("请选择 1024–65535 内、不同于正式站点 8000 的测试端口")
    import uvicorn
    uvicorn.run(preview_app(args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
