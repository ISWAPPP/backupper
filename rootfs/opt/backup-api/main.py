import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, BackgroundTasks, Request, Query
from fastapi.responses import PlainTextResponse, FileResponse, HTMLResponse

from config import (
    INCOMING_DIR,
    WEB_BACKUPS_DIR,
    SESSION_LOG_DIR,
    AUTHORIZED_KEYS_PATH,
    BACKUP_UID,
    BACKUP_GID,
    atomic_replace_lines,
    ensure_ssh_wrapper,
)
from tasks import compress_and_cleanup

# ponytail: regex over uuid.UUID parsing — catches injection without exception flow
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# ponytail: in-memory rate limiter per IP, 1 req/10s
_last_reg: dict[str, float] = defaultdict(float)


def _validate_session_id(session_id: str) -> str:
    """Return session_id if it is a valid UUID, else raise 400."""
    if not _UUID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")
    return session_id


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_ssh_wrapper()
    # ponytail: cleanup incoming/ dirs orphaned by a crash/reboot
    for d in os.listdir(INCOMING_DIR):
        if _UUID_RE.match(d):
            shutil.rmtree(os.path.join(INCOMING_DIR, d), ignore_errors=True)
    # ponytail: cleanup orphaned auth keys — every UUID-key is stale after restart
    # since lifespan just wiped all incoming/ dirs above, no active sessions remain.
    atomic_replace_lines(AUTHORIZED_KEYS_PATH, lambda l: not re.search(r"# [0-9a-f\-]{36}$", l))
    yield


app = FastAPI(lifespan=lifespan)


def format_bytes(size: int) -> str:
    if size == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0


@app.get("/disk_space", response_class=PlainTextResponse)
async def disk_space():
    """Повертає вільний дисковий простір на партиції бекапів (байти)."""
    s = os.statvfs(WEB_BACKUPS_DIR)
    total = s.f_frsize * s.f_blocks
    free = s.f_frsize * s.f_bfree
    used = total - free
    return f"total={total}\nused={used}\nfree={free}\n"


@app.get("/agent.sh", response_class=PlainTextResponse)
async def get_agent_script():
    """Віддає клієнтський bash-скрипт"""
    script_path = os.path.join(os.path.dirname(__file__), "agent.sh")
    if os.path.exists(script_path):
        with open(script_path, "r", encoding="utf-8") as f:
            return f.read()
    raise HTTPException(status_code=404, detail="Script not found")


@app.post("/register_key", response_class=PlainTextResponse)
async def register_key(request: Request, key: str = Form(...)):
    """Реєструє публічний ключ та ізолює клієнта"""
    # ponytail: rate limit 1 req/10s per IP
    now = time.time()
    if now - _last_reg[request.client.host] < 10:
        raise HTTPException(status_code=429, detail="Too many registration attempts")
    _last_reg[request.client.host] = now

    # ponytail: server-side disk guard, не покладатись на клієнта
    s = os.statvfs(INCOMING_DIR)
    if s.f_frsize * s.f_bfree < 1073741824:  # 1 GB
        raise HTTPException(status_code=503, detail="Server disk is full")

    # ponytail: strip newlines to prevent authorized_keys injection
    key = key.strip().replace("\n", "").replace("\r", "")
    if not (key.startswith("ssh-") or key.startswith("ecdsa-")):
        raise HTTPException(status_code=400, detail="Invalid SSH public key format")
    if len(key) > 4096:
        raise HTTPException(status_code=400, detail="SSH public key too long")

    session_id = str(uuid.uuid4())
    session_dir = os.path.join(INCOMING_DIR, session_id)

    os.makedirs(session_dir, exist_ok=True)
    os.chown(session_dir, BACKUP_UID, BACKUP_GID)

    rsync_command = (
        f'command="/home/backupuser/ssh-wrapper.sh {session_dir}",'
        f"no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc"
    )
    auth_line = f"{rsync_command} {key.strip()} # {session_id}\n"

    with open("/home/backupuser/.ssh/authorized_keys", "a", encoding="utf-8") as f:
        f.write(auth_line)

    return session_id


@app.post("/create_archive", response_class=PlainTextResponse)
async def create_archive(
    request: Request,
    session_id: str = Form(...),
    log: str = Form(""),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    _validate_session_id(session_id)
    session_dir = os.path.join(INCOMING_DIR, session_id)

    if not os.path.exists(session_dir):
        raise HTTPException(status_code=404, detail="Session not found or already archived")

    if log:
        log_path = os.path.join(SESSION_LOG_DIR, f"{session_id}.log")
        os.makedirs(SESSION_LOG_DIR, exist_ok=True)
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(log)
        except OSError:
            pass

    background_tasks.add_task(compress_and_cleanup, session_id)
    # ponytail: scheme from the actual request — client may come via http://IP:8000 or https://domain
    return f"{request.url.scheme}://{request.headers.get('host', request.url.hostname)}/status/{session_id}\n"


@app.post("/cancel_session", response_class=PlainTextResponse)
async def cancel_session(session_id: str = Form(...)):
    """Скасовує сесію: видаляє файли в incoming та ключ SSH."""
    _validate_session_id(session_id)

    # ponytail: rm -rf incoming/, auth_key line, and any status/size leftovers
    shutil.rmtree(os.path.join(INCOMING_DIR, session_id), ignore_errors=True)

    comment = f"# {session_id}"
    atomic_replace_lines(AUTHORIZED_KEYS_PATH, lambda l: comment not in l)

    for ext in (".status", ".size"):
        p = os.path.join(WEB_BACKUPS_DIR, f"{session_id}{ext}")
        if os.path.exists(p):
            os.remove(p)

    return "ok\n"


@app.post("/clear_backups", response_class=PlainTextResponse)
async def clear_backups(dry_run: bool = Query(False)):
    """Видаляє всі бекапи, вхідні дані та зареєстровані ключі."""
    raise HTTPException(status_code=403, detail="Disabled for safety")
    n_archives = 0
    for f in os.listdir(WEB_BACKUPS_DIR):
        fpath = os.path.join(WEB_BACKUPS_DIR, f)
        if os.path.isfile(fpath) and (
            f.endswith(".tar.gz") or f.endswith(".status") or f.endswith(".size")
        ):
            if not dry_run:
                os.remove(fpath)
            n_archives += 1

    n_incoming = 0
    for d in os.listdir(INCOMING_DIR):
        if _UUID_RE.match(d):
            if not dry_run:
                shutil.rmtree(os.path.join(INCOMING_DIR, d), ignore_errors=True)
            n_incoming += 1

    n_keys = 0
    if os.path.exists(AUTHORIZED_KEYS_PATH):
        with open(AUTHORIZED_KEYS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # ponytail: lines with trailing `# <uuid>` comment = backup keys
        new_lines = [l for l in lines if not re.search(r"# [0-9a-f\-]{36}$", l)]
        n_keys = len(lines) - len(new_lines)
        if n_keys and not dry_run:
            atomic_replace_lines(AUTHORIZED_KEYS_PATH, lambda l: not re.search(r"# [0-9a-f\-]{36}$", l))

    return f"archives={n_archives} incoming={n_incoming} keys={n_keys}\n"


@app.get("/status/{session_id}", response_class=HTMLResponse)
async def get_status(session_id: str):
    _validate_session_id(session_id)
    archive_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.tar.gz")
    status_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.status")
    size_path = os.path.join(WEB_BACKUPS_DIR, f"{session_id}.size")

    if os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8") as f:
            current_status = f.read().strip()

        if current_status == "processing":
            archive_size = os.path.getsize(archive_path) if os.path.exists(archive_path) else 0
            total_raw_size = 0
            if os.path.exists(size_path):
                try:
                    with open(size_path, "r", encoding="utf-8") as f:
                        total_raw_size = int(f.read().strip())
                except (ValueError, OSError):
                    pass

            # ponytail: progress = archive_size / raw_size, no heuristic.
            # For incompressible data (videos, archives) this matches real progress.
            # For compressible data the jump from <100% to "done" is expected.
            progress_pct = 0
            if total_raw_size > 0:
                progress_pct = min(100, int((archive_size / total_raw_size) * 100))

            archive_str = format_bytes(archive_size)
            total_str = format_bytes(total_raw_size)

            return f"""\
            <html>
                <head>
                    <meta http-equiv="refresh" content="3">
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width,initial-scale=1.0">
                    <title>Preparing backup - {progress_pct}%</title>
                    <style>
                        *{{margin:0;padding:0;box-sizing:border-box}}
                        body{{min-height:100dvh;display:grid;place-items:center;font-family:system-ui,-apple-system,sans-serif;background:#f4f5f3;padding:20px}}
                        .card{{width:min(420px,92vw);padding:2.5rem 2rem;background:#fff;border-top:3px solid #6a8a6a;border-left:1px solid #e0e2de;border-right:1px solid #e0e2de;border-bottom:1px solid #e0e2de;text-align:center}}
                        h1{{font-size:1.25rem;font-weight:600;color:#1a1d1a;margin-bottom:.35rem;letter-spacing:-.015em}}
                        p{{font-size:.85rem;color:#5a605a;line-height:1.5;margin-bottom:1.5rem}}
                        .bar{{height:5px;background:#e2e4e0;overflow:hidden;margin-bottom:.6rem}}
                        .bar-fill{{height:100%;width:{progress_pct}%;background:#6a8a6a;transition:width .3s ease}}
                        .meta{{display:flex;justify-content:space-between;font-size:.8rem;color:#5a605a;margin-bottom:1.5rem}}
                        .pct{{font-weight:600;color:#1a1d1a}}
                        .info{{display:flex;justify-content:space-between;padding-top:.75rem;border-top:1px solid #e0e2de;font-size:.75rem;color:#7a807a;font-variant-numeric:tabular-nums}}
                        .note{{margin-top:1rem;font-size:.7rem;color:#8a908a}}
                    </style>
                </head>
                <body>
                    <div class="card" role="status" aria-label="Archive progress">
                        <h1>Preparing archive</h1>
                        <p>Your download will start automatically when ready</p>
                        <div class="bar" aria-hidden="true"><div class="bar-fill"></div></div>
                        <div class="meta">
                            <span class="pct">{progress_pct}%</span>
                            <span>{archive_str} / {total_str}</span>
                        </div>
                        <div class="info">
                            <span>backup-{session_id[:8]}</span>
                            <span>refreshes every 3s</span>
                        </div>
                        <div class="note">Download starts when compression completes</div>
                    </div>
                </body>
            </html>
            """
        elif current_status == "error":
            return HTMLResponse(
                """\
            <html>
                <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Archive failed</title>
                <style>
                    *{margin:0;padding:0;box-sizing:border-box}
                    body{min-height:100dvh;display:grid;place-items:center;font-family:system-ui,-apple-system,sans-serif;background:#f4f5f3;padding:20px}
                    .card{width:min(400px,92vw);padding:2.5rem 2rem;background:#fff;border-top:3px solid #8a4a4a;border-left:1px solid #e0e2de;border-right:1px solid #e0e2de;border-bottom:1px solid #e0e2de;text-align:center}
                    h1{font-size:1.15rem;font-weight:600;color:#6a3a3a;margin-bottom:.5rem}
                    p{font-size:.85rem;color:#6a6060;line-height:1.5}
                </style></head>
                <body>
                    <div class="card" role="alert">
                        <h1>Archive failed</h1>
                        <p>Something went wrong during compression.<br>Try again or contact the administrator.</p>
                    </div>
                </body>
            </html>
            """,
                status_code=500,
            )

    if os.path.exists(archive_path):
        return FileResponse(
            path=archive_path,
            filename=f"backup_{session_id[:8]}.tar.gz",
            media_type="application/gzip",
        )

    return HTMLResponse(
        """\
        <html>
            <head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Archive not found</title>
            <style>
                *{margin:0;padding:0;box-sizing:border-box}
                body{min-height:100dvh;display:grid;place-items:center;font-family:system-ui,-apple-system,sans-serif;background:#f4f5f3;padding:20px}
                .card{width:min(400px,92vw);padding:2.5rem 2rem;background:#fff;border-top:3px solid #5a6a5a;border-left:1px solid #e0e2de;border-right:1px solid #e0e2de;border-bottom:1px solid #e0e2de;text-align:center}
                h1{font-size:1.15rem;font-weight:600;color:#4a5a4a;margin-bottom:.5rem}
                p{font-size:.85rem;color:#5a6a5a;line-height:1.5}
            </style></head>
            <body>
                <div class="card" role="status">
                    <h1>Archive not found</h1>
                    <p>The session does not exist or the archive was already downloaded and removed.</p>
                </div>
            </body>
        </html>
        """,
        status_code=404,
    )
