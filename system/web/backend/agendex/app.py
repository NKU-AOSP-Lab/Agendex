"""Agendex HTTP API，支持本地工作区和带账号保护的 HTTPS 部署。"""

from contextlib import asynccontextmanager
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .db import connection, database_path, initialize_database
from .evidence import add_evidence
from .jobs import JobEngine
from .schemas import EvidenceInput, JobInput, PolicyInput, ThresholdInput
from . import annotations, exports, services
from .scoring import backfill_scores
from .jev import JevError
from .policy_generation import GenerationInput, generate_policy
from .auth import AuthConfig, AuthManager, COOKIE_NAME, LoginInput, protected_path


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
DEFAULT_ORIGIN_PORTS = {8000, 5173}


def local_ports() -> set[int]:
    configured = os.environ.get("AGENDEX_LOCAL_PORTS")
    if configured is None:
        return DEFAULT_ORIGIN_PORTS
    try:
        ports = {int(value.strip()) for value in configured.split(",")}
        return ports if ports and all(1 <= value <= 65535 for value in ports) else set()
    except ValueError:
        return set()


def local_origin(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port in local_ports()
    except ValueError:
        return False


def create_app(db_path=None, client=None, start_workers: bool = True, glm_client=None) -> FastAPI:
    auth = AuthManager(AuthConfig.from_environment())
    target = Path(db_path) if db_path is not None else database_path()
    initialize_database(target)
    with connection(target) as conn:
        backfill_scores(conn)
    engine = JobEngine(target, client=client, glm_client=glm_client)

    @asynccontextmanager
    async def lifespan(app):
        engine.recover()
        if start_workers:
            engine.start()
        try:
            yield
        finally:
            if start_workers:
                engine.stop()

    app = FastAPI(title="Agendex", version="0.1.0", lifespan=lifespan)
    app.state.db_path = target
    app.state.engine = engine
    app.state.auth = auth

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        writing = request.method not in {"GET", "HEAD", "OPTIONS"}
        if auth.config.enabled:
            if not auth.trusted_request(request) or (writing and not auth.trusted_origin(request.headers.get("origin"))):
                return JSONResponse({"detail": "仅允许本站发起请求"}, status_code=403, headers={"Cache-Control": "no-store"})
            if protected_path(request.url.path) and not auth.session(request.cookies.get(COOKIE_NAME))["authenticated"]:
                return JSONResponse({"detail": "请登录后继续"}, status_code=401, headers={"Cache-Control": "no-store"})
        elif writing:
            # 防止外站表单/脚本与 DNS rebinding 触发本地付费任务。
            try:
                host = urlsplit("//" + request.headers.get("host", "")).hostname
            except ValueError:
                host = None
            origin = request.headers.get("origin")
            if host not in ALLOWED_HOSTS or (origin is not None and not local_origin(origin)):
                return JSONResponse({"detail": "仅允许本地页面发起写入请求"}, status_code=403)
        response = await call_next(request)
        if auth.config.enabled and (request.url.path.startswith("/api/") or protected_path(request.url.path)):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/session")
    def auth_session(request: Request):
        return auth.session(request.cookies.get(COOKIE_NAME))

    @app.post("/api/auth/login")
    def auth_login(request: Request, body: LoginInput, response: Response):
        if not auth.config.enabled:
            return auth.session(None)
        address = request.client.host if request.client else "unknown"
        if not auth.allow_attempt(address):
            return JSONResponse({"detail": "尝试次数过多，请稍后再试"}, status_code=429, headers={"Retry-After": "60"})
        if not auth.verify_credentials(body.username, body.password):
            return JSONResponse({"detail": "账号或密码不正确"}, status_code=401)
        return auth.issue_session(response, address)

    @app.post("/api/auth/logout")
    def auth_logout(response: Response):
        auth.clear_session(response)
        return {"enabled": auth.config.enabled, "authenticated": not auth.config.enabled, "username": None}

    @app.exception_handler(services.ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(JevError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.get("/api/health")
    def health():
        models = engine.model_options()
        return {"status": "ok", "jev_configured": models[0]["configured"],
                "glm_configured": models[1]["configured"], "model": engine.client.model, "models": models}

    @app.get("/api/stats")
    def stats():
        with connection(target) as conn:
            return services.get_stats(conn)

    @app.get("/api/repositories")
    def repositories(q: str = Query("", max_length=300), language: str | None = None,
                     min_stars: int = Query(0, ge=0), sort: str = "stars", page: int = Query(1, ge=1),
                     page_size: int = Query(25, ge=1, le=100), policy_id: str | None = None, tag_id: str | None = None,
                     retention_source: str | None = None, tag_ids: str = Query("", max_length=3239)):
        with connection(target) as conn:
            return services.list_repositories(conn, {"q": q, "language": language, "min_stars": min_stars, "sort": sort, "policy_id": policy_id, "tag_id": tag_id, "tag_ids": tag_ids.split(",") if tag_ids else [], "retention_source": retention_source}, page, page_size)

    @app.get("/api/repositories/export.csv")
    def export_repositories():
        with connection(target) as conn:
            conn.execute("BEGIN")
            content = exports.repositories_csv(conn)
        return Response(content, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="agendex-repositories.csv"',
        })

    @app.get("/api/repositories/{identifier}")
    def repository(identifier: int):
        with connection(target) as conn:
            return services.get_repository(conn, identifier)

    @app.get("/api/repositories/{identifier}/annotations")
    def repository_annotations(identifier: int):
        with connection(target) as conn:
            return annotations.repository_annotations(conn, identifier)

    @app.put("/api/repositories/{identifier}/annotations")
    def update_annotations(identifier: int, body: annotations.AnnotationInput):
        with connection(target) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return annotations.replace_annotations(conn, identifier, body)

    @app.post("/api/repositories/{identifier}/evidence", status_code=201)
    def evidence(identifier: int, body: EvidenceInput):
        with connection(target) as conn:
            if conn.execute("SELECT 1 FROM repositories WHERE id=?", (identifier,)).fetchone() is None:
                raise services.ServiceError("仓库不存在", 404)
            try:
                return add_evidence(conn, identifier, body)
            except ValueError as exc:
                raise services.ServiceError(str(exc)) from None

    @app.get("/api/policies")
    def policies():
        with connection(target) as conn:
            return {"items": services.list_policies(conn)}

    @app.post("/api/policies/generate")
    def draft_policy(body: GenerationInput):
        # Only this explicit request drafts a strategy; saving and classification
        # remain separate user actions. No database transaction spans the network.
        return generate_policy(target, body, client=engine.glm_client)

    @app.get("/api/policies/{identifier}")
    def policy(identifier: str):
        with connection(target) as conn:
            return services.get_policy(conn, identifier)

    @app.post("/api/policies", status_code=201)
    def create_policy(body: PolicyInput):
        with connection(target) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return services.save_policy(conn, body, request_model=engine.client.model)

    @app.put("/api/policies/{identifier}")
    def update_policy(identifier: str, body: PolicyInput):
        with connection(target) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return services.save_policy(conn, body, identifier, request_model=engine.client.model)

    @app.get("/api/policies/{identifier}/versions")
    def versions(identifier: str):
        with connection(target) as conn:
            return {"items": services.policy_versions(conn, identifier)}

    @app.get("/api/policies/{identifier}/results")
    def results(identifier: str, revision: int | None = Query(None, ge=1), q: str = Query("", max_length=300),
                page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        with connection(target) as conn:
            return services.policy_results(conn, identifier, revision, q, page, page_size)

    @app.get("/api/policies/{identifier}/results/export.csv")
    def export_results(identifier: str, revision: int | None = Query(None, ge=1),
                       q: str = Query("", max_length=300), thresholds: str | None = Query(None, max_length=10000)):
        with connection(target) as conn:
            conn.execute("BEGIN")
            content, selected_revision = exports.policy_results_csv(conn, identifier, revision, q, thresholds)
        # 策略 ID 正常为 UUID；请求路径仍只允许安全字符进入下载文件名。
        safe_id = "".join(character for character in identifier if character.isascii() and (character.isalnum() or character in "-_"))[:80] or "policy"
        filename = f"agendex-policy-{safe_id}-v{selected_revision}-results.csv"
        return Response(content, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        })

    @app.post("/api/policies/{identifier}/thresholds")
    def thresholds(identifier: str, body: ThresholdInput):
        with connection(target) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return services.update_thresholds(conn, identifier, body, engine.client.model)

    @app.post("/api/policies/{identifier}/restore/{revision}")
    def restore(identifier: str, revision: int):
        with connection(target) as conn:
            conn.execute("BEGIN IMMEDIATE")
            return services.restore_policy(conn, identifier, revision, engine.client.model)

    @app.delete("/api/policies/{identifier}")
    def delete_policy(identifier: str):
        with connection(target) as conn:
            services.archive_policy(conn, identifier)
            return {"ok": True}

    @app.get("/api/jobs")
    def jobs():
        with connection(target) as conn:
            return {"items": services.list_jobs(conn)}

    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobInput):
        return engine.create_job(body)

    @app.get("/api/jobs/{identifier}")
    def job(identifier: str):
        with connection(target) as conn:
            return services.get_job(conn, identifier, detail=True)

    @app.post("/api/jobs/{identifier}/{action}")
    def job_action(identifier: str, action: str):
        return engine.control(identifier, action)

    # API 优先注册；构建后可直接由同一个本地服务提供网页。
    static = Path(__file__).resolve().parents[2] / "dist"
    if static.is_dir():
        app.mount("/", StaticFiles(directory=str(static), html=True), name="web")
    return app


app = create_app()
