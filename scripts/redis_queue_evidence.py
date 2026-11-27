#!/usr/bin/env python
"""Real Redis export-queue evidence: real enqueue -> real worker -> real file -> DB status.

Env (never commit real values):
  ASST_DATABASE_URL, ASST_REDIS_URL, ASST_TRAIL_KEY, optional ASST_E2E_SCHEMA
Usage: python scripts/redis_queue_evidence.py
Output: reports/redis-queue-e2e.log (redacted) + last line EXIT=0/1
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "redis-queue-e2e.log"

ORG = uuid.uuid4()
SITE = uuid.uuid4()
EMPLOYEE = uuid.uuid4()
AUDITOR = uuid.uuid4()
EMPLOYEE_TOKEN = "e2e-redis-employee-token"
AUDITOR_TOKEN = "e2e-redis-auditor-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def redact(text: str, secrets: list) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1<password>\2", text)


def with_search_path(dsn: str, schema: str) -> str:
    """Append libpq options=-c search_path=<schema> to a SQLAlchemy DSN.

    不能用 urlsplit 直接拆：口令里若含 '#' 或 '@'，urlsplit 会把后半段当成 fragment
    或第二个 userinfo，拆出来的 netloc 是残的（真实口令就踩过这个坑）。
    这里手工按 '://' 与第一个 '@' 切，再对 user:password 段整体做百分号编码。
    """
    scheme, _, rest = dsn.partition("://")
    if not rest:
        raise ValueError("DSN 缺少 scheme://")
    authority, _, tail = rest.partition("/")
    if "@" in authority:
        userinfo, _, hostport = authority.rpartition("@")
        authority = quote(userinfo, safe=":") + "@" + hostport
    query = tail.partition("?")[2] if "?" in tail else ""
    path = tail.partition("?")[0]
    extra = "options=" + quote("-c search_path=" + schema, safe="")
    query = (query + "&" + extra) if query else extra
    return scheme + "://" + authority + "/" + path + "?" + query


def wait_ready(base_url: str, timeout: float = 90.0) -> None:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base_url + "/healthz", timeout=2.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("uvicorn not ready in time")


def main() -> int:
    import httpx
    import redis

    base_dsn = (os.environ.get("ASST_DATABASE_URL") or "").strip()
    redis_url = (os.environ.get("ASST_REDIS_URL") or "").strip()
    trail_key = (os.environ.get("ASST_TRAIL_KEY") or "").strip()
    missing = []
    if not base_dsn:
        missing.append("ASST_DATABASE_URL")
    if not redis_url:
        missing.append("ASST_REDIS_URL")
    if not trail_key:
        missing.append("ASST_TRAIL_KEY")
    if missing:
        print("missing env: " + ", ".join(missing), file=sys.stderr)
        return 2

    is_pg = base_dsn.startswith("postgresql")
    schema = ""
    if is_pg:
        schema = (os.environ.get("ASST_E2E_SCHEMA") or "asst_e2e_" + uuid.uuid4().hex[:10]).strip()

    secrets = [base_dsn, redis_url, trail_key]
    export_dir = ROOT / "var" / "e2e-redis-exports"
    if export_dir.exists():
        for stale in export_dir.glob("*.jsonl"):
            stale.unlink()
    export_dir.mkdir(parents=True, exist_ok=True)

    port = free_port()
    base_url = "http://127.0.0.1:" + str(port)
    key_prefix = "asst1853:e2e-" + uuid.uuid4().hex[:8] + ":"
    queue_key = key_prefix + "export:queue"
    processing_key = key_prefix + "export:processing"

    env = dict(os.environ)
    env["ASST_EXPORT_QUEUE"] = "redis"
    env["ASST_REDIS_KEY_PREFIX"] = key_prefix
    env["ASST_EXPORT_DIR"] = str(export_dir)
    env["ASST_EXPORT_SIGNING_KEY"] = uuid.uuid4().hex
    env["ASST_PUBLIC_BASE_URL"] = base_url
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    employee_entry = ":".join([EMPLOYEE_TOKEN, str(EMPLOYEE), str(ORG), "employee", str(SITE)])
    auditor_entry = ":".join([AUDITOR_TOKEN, str(AUDITOR), str(ORG), "auditor", ""])
    env["ASST_IAM_STUB_TOKENS"] = ";".join([employee_entry, auditor_entry])
    env["ASST_DATABASE_URL"] = with_search_path(base_dsn, schema) if is_pg else base_dsn

    lines = []

    def emit(text: str = "") -> None:
        lines.append(redact(text, secrets))

    emit("# Redis 导出队列真机端到端取证")
    emit()
    emit("- 日期：" + time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    emit("- 数据库：" + ("真实 PostgreSQL（临时 schema " + schema + "）" if is_pg else "SQLite 文件"))
    emit("- 队列：真实 Redis（ASST_EXPORT_QUEUE=redis，键前缀 " + key_prefix + "）")
    emit("- worker：独立进程 python -m app.workers.export_worker --max-jobs 1 --once")
    emit()

    admin = None
    if is_pg:
        import psycopg

        admin = psycopg.connect(
            base_dsn.replace("postgresql+psycopg://", "postgresql://"),
            autocommit=True,
            connect_timeout=8,
        )
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS " + chr(34) + schema + chr(34) + " CASCADE")
            cur.execute("CREATE SCHEMA " + chr(34) + schema + chr(34))

    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5)
    ok = False
    keys_after = []
    try:
        emit("## 1. Redis 连通性")
        emit()
        emit("PING -> " + str(client.ping()))
        emit("redis_version = " + str(client.info("server").get("redis_version")))
        emit("LLEN " + queue_key + " (before) = " + str(client.llen(queue_key)))
        emit()

        emit("## 2. alembic upgrade head")
        emit()
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        emit(proc.stdout or "")
        emit(proc.stderr or "")
        emit("exit code = " + str(proc.returncode))
        emit()
        if proc.returncode != 0:
            raise RuntimeError("alembic upgrade failed")

        emit("## 3. 以 ASST_EXPORT_QUEUE=redis 启动 API")
        emit()
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        server_lines = []

        def _drain(stream) -> None:
            try:
                for line in stream:
                    server_lines.append(line.rstrip("\n"))
            except Exception:
                pass

        threading.Thread(target=_drain, args=(server.stdout,), daemon=True).start()
        try:
            wait_ready(base_url)
            emit("GET /healthz -> 200")
            emit()
            api = httpx.Client(base_url=base_url, timeout=30.0)
            emp = {"Authorization": "Bearer " + EMPLOYEE_TOKEN}
            aud = {"Authorization": "Bearer " + AUDITOR_TOKEN}

            emit("## 4. 通过真实 API 造 5 条留痕")
            emit()
            conv = api.post(
                "/api/assistant/conversations",
                headers=emp,
                json={"title": "Redis E2E 虚构会话", "site_id": str(SITE)},
            ).json()["id"]
            for index in range(5):
                resp = api.post(
                    "/api/assistant/conversations/" + conv + "/messages",
                    headers=emp,
                    json={"content": "redis 队列测试消息 " + str(index)},
                )
                assert resp.status_code == 201, resp.text
            emit("- conversation_id = " + conv)
            emit("- 5 x POST /api/assistant/conversations/{id}/messages -> 201")
            emit()

            emit("## 5. POST /api/assistant/audit/exports（API 只入队）")
            emit()
            created = api.post("/api/assistant/audit/exports", headers=aud, json={})
            body = created.json()
            job_id = body["job_id"]
            emit(json.dumps(body, ensure_ascii=False, indent=2))
            emit("- HTTP " + str(created.status_code) + "（期望 202），status = " + str(body.get("status")))
            files_before = [p.name for p in export_dir.glob("*.jsonl")]
            queue_depth = client.llen(queue_key)
            queued_ids = client.lrange(queue_key, 0, -1)
            emit("- worker 未跑时磁盘上的文件：" + json.dumps(files_before) + "（期望 []）")
            emit("- LLEN " + queue_key + " = " + str(queue_depth))
            emit("- LRANGE queue = " + json.dumps(queued_ids))
            emit()
            ok_enqueue = (
                created.status_code == 202
                and body.get("status") == "queued"
                and files_before == []
                and queue_depth == 1
                and queued_ids == [job_id]
            )

            emit("## 6. worker 独立进程消费队列")
            emit()
            worker = subprocess.run(
                [sys.executable, "-m", "app.workers.export_worker", "--max-jobs", "1", "--once"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            emit("$ python -m app.workers.export_worker --max-jobs 1 --once")
            emit(worker.stdout or "")
            emit(worker.stderr or "")
            emit("exit code = " + str(worker.returncode))
            emit()
            queue_after = client.llen(queue_key)
            processing_after = client.lrange(processing_key, 0, -1)
            emit("- LLEN queue after worker = " + str(queue_after) + "（期望 0）")
            emit("- processing 列表 after worker = " + json.dumps(processing_after) + "（期望 []）")
            emit()

            emit("## 7. 任务状态与生成文件")
            emit()
            status = api.get("/api/assistant/audit/exports/" + job_id, headers=aud)
            status_body = status.json()
            emit(json.dumps(status_body, ensure_ascii=False, indent=2)[:2000])
            files = sorted(export_dir.glob("*.jsonl"))
            emit("- worker 跑完后的文件：" + json.dumps([p.name for p in files]))
            row_lines = []
            if files:
                text = files[0].read_text(encoding="utf-8")
                row_lines = [ln for ln in text.splitlines() if ln.strip()]
                emit("- 文件行数 = " + str(len(row_lines)) + "（1 行 manifest + 5 条留痕）")
            emit()
            ok_worker = (
                worker.returncode == 0
                and queue_after == 0
                and processing_after == []
                and status_body.get("status") == "finished"
                and status_body.get("row_count") == 5
                and len(row_lines) == 6
                and bool(status_body.get("download_url"))
            )

            emit("## 8. 下载导出文件（签名链接单次可用）")
            emit()
            download = api.get(status_body["download_url"])
            emit("- GET download_url -> HTTP " + str(download.status_code) + "（期望 200）")
            emit("- bytes = " + str(len(download.content)))
            again = api.get(status_body["download_url"])
            emit("- 同一链接再取一次 -> HTTP " + str(again.status_code) + "（期望 410）")
            emit()
            ok_download = download.status_code == 200 and again.status_code == 410

            emit("## 9. 结论")
            emit()
            emit("- API 只入队：" + ("PASS" if ok_enqueue else "FAIL"))
            emit("- worker 真消费 + 真落文件 + 状态 finished：" + ("PASS" if ok_worker else "FAIL"))
            emit("- 签名下载单次可用：" + ("PASS" if ok_download else "FAIL"))
            emit()
            ok = ok_enqueue and ok_worker and ok_download
        finally:
            server.terminate()
            try:
                server.wait(timeout=15)
            except Exception:
                server.kill()
            emit("## 10. 服务端访问日志（uvicorn，末 40 行）")
            emit()
            emit("\n".join(server_lines[-40:]))
            emit()
    finally:
        client.delete(queue_key)
        client.delete(processing_key)
        keys_after = list(client.keys(key_prefix + "*"))
        client.close()

    if admin is not None:
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS " + chr(34) + schema + chr(34) + " CASCADE")
            cur.execute("select count(*) from pg_namespace where nspname = %s", (schema,))
            leftover = cur.fetchone()[0]
        admin.close()
    else:
        leftover = 0

    emit("## 11. 清理")
    emit()
    emit("- 前缀 " + key_prefix + " 下残留 key = " + json.dumps(keys_after))
    if is_pg:
        emit("- schema " + schema + " 已 DROP，残留 = " + str(leftover))
    emit()

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(redact("\n".join(lines), secrets) + "\n", encoding="utf-8", newline="\n")
    print("EXIT=0" if ok else "EXIT=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
