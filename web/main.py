from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent.models import GraphicResult, ProgressStep, SummaryResult
from agent.tools import close_genai_client
from web.auth import (
    AUTH_COOKIE_NAME,
    assert_auth_config,
    auth_enabled,
    cookie_max_age,
    create_auth_cookie,
    password_matches,
    request_is_authenticated,
)
from web.agent_client import build_agent_client
from web.logging_config import configure_logging


BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
JobKind = Literal["summary", "graphic"]
JobStatus = Literal["running", "done", "failed"]


@dataclass
class AgentJob:
    job_id: str
    kind: JobKind
    title: str
    status: JobStatus = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    progress: list = field(default_factory=list)
    summary: Optional[SummaryResult] = None
    graphic: Optional[GraphicResult] = None
    feedback: str = ""
    error: str = ""

    @property
    def elapsed_seconds(self) -> int:
        return max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds()))

    @property
    def wait_hint(self) -> str:
        if self.kind == "graphic":
            return "画像生成は 1〜3 分かかることがあります。画面は自動更新されます"
        return "記事取得と要約には 30〜90 秒かかることがあります。画面は自動更新されます"

    @property
    def slow_after_seconds(self) -> int:
        if self.kind == "graphic":
            return 240
        return 120

    @property
    def is_slow(self) -> bool:
        return self.status == "running" and self.elapsed_seconds >= self.slow_after_seconds

    @property
    def slow_message(self) -> str:
        if self.kind == "graphic":
            return "画像生成が通常より長くかかっています。数分続く場合は Agent Runtime logs と Gemini image model の quota を確認してください。"
        return "要約が通常より長くかかっています。数分続く場合は URL の本文取得可否と Agent Runtime logs を確認してください。"

    @property
    def show_estimated_progress(self) -> bool:
        return self.status == "running" and len(self.progress) <= 1

    @property
    def estimated_progress(self) -> list[ProgressStep]:
        if not self.show_estimated_progress:
            return []
        if self.kind == "graphic":
            milestones = [
                (0, "要約を Agent Runtime に送信"),
                (10, "Agent が style と構成案を判断"),
                (30, "Gemini で画像生成を実行"),
                (80, "Cloud Storage に成果物を保存"),
                (105, "signed URL を準備して画面へ返却"),
            ]
        else:
            milestones = [
                (0, "Agent Runtime に要約 workflow を送信"),
                (12, "記事本文を取得"),
                (35, "3 行要約と重要ポイントを生成"),
                (60, "JSON contract を検証して画面へ返却"),
            ]
        return _estimated_progress_steps(self.elapsed_seconds, milestones)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global agent_client
    configure_logging()
    assert_auth_config()
    agent_client = build_agent_client()
    yield
    close_genai_client()
    agent_client = None


app = FastAPI(title="Graphic Recording Agent Demo", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/artifacts", StaticFiles(directory=Path("artifacts")), name="artifacts")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
agent_client = None

sessions: dict[str, SummaryResult] = {}
graphics: dict[str, GraphicResult] = {}
jobs: dict[str, AgentJob] = {}
background_tasks: set[asyncio.Task] = set()


@app.middleware("http")
async def require_password_auth(request: Request, call_next) -> Response:
    if _is_auth_exempt_path(request.url.path) or request_is_authenticated(request):
        return await call_next(request)

    if request.method == "GET":
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"HX-Redirect": "/login"},
    )


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next_path": _safe_next_path(next), "error": ""},
    )


@app.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    next_path: str = Form("/"),
) -> Response:
    next_url = _safe_next_path(next_path)
    if not auth_enabled():
        return RedirectResponse(url=next_url, status_code=303)
    if not password_matches(password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next_path": next_url,
                "error": "パスワードが違います。",
            },
            status_code=401,
        )

    response = RedirectResponse(url=next_url, status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        create_auth_cookie(),
        max_age=cookie_max_age(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"auth_enabled": auth_enabled()},
    )


@app.post("/summaries", response_class=HTMLResponse)
async def summarize(request: Request, url: str = Form(...)) -> HTMLResponse:
    job = _create_job("summary", "記事を要約しています")
    _schedule_background_task(_run_summary_job(job.job_id, url))
    return templates.TemplateResponse(
        request,
        "partials/job.html",
        {"job": job},
    )


@app.post("/graphics", response_class=HTMLResponse)
async def create_graphic(
    request: Request,
    session_id: str = Form(...),
    summary_text: str = Form(""),
    key_points_text: str = Form(""),
) -> HTMLResponse:
    summary = _get_summary(session_id)
    _apply_summary_edits(summary, summary_text, key_points_text)
    job = _create_job("graphic", "グラレコを生成しています")
    _schedule_background_task(_run_graphic_job(job.job_id, summary, feedback=""))
    return templates.TemplateResponse(
        request,
        "partials/job.html",
        {"job": job},
    )


@app.post("/graphics/regenerate", response_class=HTMLResponse)
async def regenerate_graphic(
    request: Request,
    session_id: str = Form(...),
    feedback: str = Form(""),
) -> HTMLResponse:
    summary = _get_summary(session_id)
    job = _create_job("graphic", "フィードバックを反映しています", feedback=feedback)
    _schedule_background_task(_run_graphic_job(job.job_id, summary, feedback=feedback))
    return templates.TemplateResponse(
        request,
        "partials/job.html",
        {"job": job},
    )


@app.get("/graphics/{session_id}/download")
async def download_graphic(session_id: str) -> Response:
    graphic = graphics.get(session_id)
    if not graphic:
        raise HTTPException(status_code=404, detail="Graphic not found")

    artifact_path = Path(graphic.artifact_path)
    if artifact_path.is_file():
        return FileResponse(
            artifact_path,
            media_type=graphic.artifact_mime_type,
            filename=_download_filename(graphic),
        )

    if not _is_http_url(graphic.artifact_url):
        raise HTTPException(status_code=404, detail="Artifact not found")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(graphic.artifact_url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning(
            "Graphic artifact download from URL failed: session_id=%s url=%s error=%s",
            session_id,
            graphic.artifact_url,
            exc,
        )
        raise HTTPException(status_code=404, detail="Artifact not found") from exc

    return Response(
        content=response.content,
        media_type=response.headers.get("content-type") or graphic.artifact_mime_type,
        headers={"Content-Disposition": f'attachment; filename="{_download_filename(graphic)}"'},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def poll_job(request: Request, job_id: str) -> HTMLResponse:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status == "done" and job.kind == "summary" and job.summary:
        return _retarget_job_response(
            templates.TemplateResponse(
                request,
                "partials/summary.html",
                {"summary": job.summary},
            ),
            job.job_id,
            request,
        )

    if job.status == "done" and job.kind == "graphic" and job.summary and job.graphic:
        return _retarget_job_response(
            templates.TemplateResponse(
                request,
                "partials/graphic.html",
                {
                    "summary": job.summary,
                    "graphic": job.graphic,
                    "feedback": job.feedback,
                },
            ),
            job.job_id,
            request,
        )

    if job.status == "failed":
        return _retarget_job_response(
            templates.TemplateResponse(
                request,
                "partials/job.html",
                {"job": job},
            ),
            job.job_id,
            request,
        )

    if request.headers.get("HX-Target") == f"{job.job_id}-content":
        return templates.TemplateResponse(
            request,
            "partials/job_content.html",
            {"job": job},
        )

    return templates.TemplateResponse(
        request,
        "partials/job.html",
        {"job": job},
    )


def _get_summary(session_id: str) -> SummaryResult:
    summary = sessions.get(session_id)
    if not summary:
        raise HTTPException(status_code=404, detail="Session not found")
    return summary


def _create_job(kind: JobKind, title: str, feedback: str = "") -> AgentJob:
    job_id = f"{kind}-{uuid4().hex}"
    job = AgentJob(job_id=job_id, kind=kind, title=title, feedback=feedback)
    jobs[job_id] = job
    return job


def _retarget_job_response(response: HTMLResponse, job_id: str, request: Request) -> HTMLResponse:
    if request.headers.get("HX-Request") == "true":
        response.headers["HX-Retarget"] = f"#{job_id}"
        response.headers["HX-Reswap"] = "outerHTML"
    return response


def _download_filename(graphic: GraphicResult) -> str:
    suffix = Path(graphic.artifact_path).suffix or ".bin"
    return f"graphic-recording-{graphic.session_id[:8]}{suffix}"


def _is_http_url(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


def _schedule_background_task(coro) -> None:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _run_summary_job(job_id: str, url: str) -> None:
    job = jobs[job_id]
    started = time.perf_counter()

    async def update_progress(progress: list) -> None:
        job.progress = progress

    try:
        summary = await _agent_client().summarize_url(url, on_progress=update_progress)
    except Exception as exc:
        logger.exception("Summary job failed: job_id=%s url=%s", job_id, url)
        job.status = "failed"
        job.error = _display_error(exc)
        return

    sessions[summary.session_id] = summary
    job.summary = summary
    job.progress = summary.progress
    job.status = "done"
    _log_job_duration("summary", job_id, started)


async def _run_graphic_job(job_id: str, summary: SummaryResult, feedback: str) -> None:
    job = jobs[job_id]
    job.summary = summary
    started = time.perf_counter()

    async def update_progress(progress: list) -> None:
        job.progress = progress

    try:
        if feedback:
            graphic = await _agent_client().regenerate_graphic_recording(
                summary,
                feedback,
                on_progress=update_progress,
            )
        else:
            graphic = await _agent_client().generate_graphic_recording(
                summary,
                on_progress=update_progress,
            )
    except Exception as exc:
        logger.exception("Graphic job failed: job_id=%s session_id=%s", job_id, summary.session_id)
        job.status = "failed"
        job.error = _display_error(exc)
        return

    graphics[summary.session_id] = graphic
    job.graphic = graphic
    job.progress = graphic.progress
    job.status = "done"
    _log_job_duration("graphic", job_id, started)


def _apply_summary_edits(summary: SummaryResult, summary_text: str, key_points_text: str) -> None:
    summary_lines = [line.strip() for line in summary_text.splitlines() if line.strip()]
    key_points = [line.strip() for line in key_points_text.splitlines() if line.strip()]
    if summary_lines:
        summary.summary_lines = summary_lines[:3]
    if key_points:
        summary.key_points = key_points[:6]


def _display_error(exc: Exception) -> str:
    raw_message = str(exc).strip()
    technical_detail = raw_message or f"{type(exc).__name__}: {exc!r}"
    friendly_message = _friendly_error_message(technical_detail)
    if friendly_message == technical_detail:
        return friendly_message
    return f"{friendly_message}\n技術詳細: {technical_detail}"


def _friendly_error_message(message: str) -> str:
    normalized = message.lower()
    if "project_number" in normalized or "resource_id" in normalized:
        return "Agent Runtime の resource name に placeholder が残っています。AGENT_RUNTIME_RESOURCE_NAME を実際の projects/.../reasoningEngines/... に設定して Cloud Run を再 deploy してください。"
    if "publisher model" in normalized or ("model" in normalized and "404" in normalized):
        return "Gemini model が見つかりません。model ID と GOOGLE_CLOUD_LOCATION=global の設定を確認してください。"
    if "signed url" in normalized or "signblob" in normalized or "serviceaccounttokencreator" in normalized:
        return "画像の signed URL 生成権限が不足しています。configure-runtime-iam.sh を再実行してください。"
    if "permission" in normalized or "403" in normalized or "denied" in normalized:
        return "Google Cloud の権限が不足しています。Cloud Run / Agent Runtime / Cloud Storage の IAM 設定を確認してください。"
    if "agent runtime returned no workflow response" in normalized or "assertionerror" in normalized:
        return "Agent Runtime から期待した形式の応答が返りませんでした。Runtime logs を確認してください。"
    if "gcs_bucket is required" in normalized:
        return "生成画像の保存先 bucket が設定されていません。GCS_BUCKET を設定して Runtime を再 deploy してください。"
    if "exceeds" in normalized and "bytes" in normalized:
        return "記事本文が大きすぎるため取得を停止しました。別の記事 URL で試してください。"
    if "url" in normalized or "fetch" in normalized or "article" in normalized:
        return "記事本文を取得できませんでした。公開されている記事 URL か、別の URL で試してください。"
    return message


def _estimated_progress_steps(elapsed_seconds: int, milestones: list[tuple[int, str]]) -> list[ProgressStep]:
    steps: list[ProgressStep] = []
    for index, (starts_at, label) in enumerate(milestones):
        next_starts_at = milestones[index + 1][0] if index + 1 < len(milestones) else None
        if elapsed_seconds < starts_at:
            status = "pending"
        elif next_starts_at is not None and elapsed_seconds >= next_starts_at:
            status = "done"
        else:
            status = "running"
        detail = "目安ステップです。実際の結果は Agent Runtime 完了後に反映されます"
        steps.append(ProgressStep(label, status, detail))
    return steps


def _is_auth_exempt_path(path: str) -> bool:
    return path in {"/login", "/healthz"} or path.startswith("/static/")


def _safe_next_path(path: str) -> str:
    # Minimal open redirect guard: only same-origin absolute paths are allowed.
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    if path.startswith("/login"):
        return "/"
    return path


def _agent_client():
    global agent_client
    if agent_client is None:
        agent_client = build_agent_client()
    return agent_client


def _log_job_duration(kind: JobKind, job_id: str, started: float) -> None:
    logger.info(
        "job_duration kind=%s job_id=%s elapsed_seconds=%.3f",
        kind,
        job_id,
        time.perf_counter() - started,
    )
