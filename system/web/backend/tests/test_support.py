"""网站测试夹具。仅在临时 SQLite 中创建合成仓库，不调用远程服务。"""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from agendex.db import connection, initialize_database, now
from agendex.evidence import encode


def isolated_app_factory():
    """隔离 app 模块默认实例的初始化；各测试另传自己的业务夹具库。"""
    with tempfile.TemporaryDirectory(prefix="agendex-test-bootstrap-") as directory:
        with patch.dict(os.environ, {"AGENDEX_DB_PATH": str(Path(directory) / "bootstrap.sqlite3")}):
            from agendex.app import create_app
    return create_app


def create_fixture(path, count=8):
    initialize_database(path)
    with connection(path) as conn:
        for identifier in range(1, count + 1):
            meta = {"id": identifier, "full_name": "example/repo{}".format(identifier),
                    "description": "A coding and browser agent {}".format(identifier),
                    "topics": ["ai-agents", "coding"], "language": "Python" if identifier % 2 else "TypeScript"}
            conn.execute("INSERT INTO repositories(id,full_name,url,description,stars,language,topics_json,agent_probability,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (identifier, meta["full_name"], "https://github.com/" + meta["full_name"], meta["description"], identifier * 10, meta["language"], encode(meta["topics"]), .9, now()))
            text = encode(meta)
            conn.execute("INSERT INTO evidence(id,repository_id,kind,version,content,content_hash,captured_at) VALUES(?,?,?,?,?,?,?)",
                         ("evidence-{}".format(identifier), identifier, "metadata", "fixture-snapshot", text, hashlib.sha256(text.encode()).hexdigest(), now()))


def policy_body(**overrides):
    return {"name": "用途", "description": "Only explicit repository evidence qualifies.",
            "mode": "multi_label", "threshold": .8,
            "tags": [{"id": "coding", "label": "代码开发", "description": "The agent develops or modifies software."},
                     {"id": "browser", "label": "网页浏览", "description": "The agent operates a browser."}],
            "evidence_kinds": ["metadata"], **overrides}


class FakeJev:
    model = "fixture/requested-model"

    def __init__(self, handler=None):
        self.handler = handler
        self.requests = []

    def configured(self):
        return True

    def request(self, payload):
        self.requests.append(payload)
        if self.handler:
            return self.handler(payload)
        return {"model": "fixture/served-model", "usage": {"input_tokens": 100, "output_tokens": 5, "cost": .001},
                "answers": {key: {"type": "noul", "noul": .92 if "coding" in key else .2} for key in payload["questions"]}}
