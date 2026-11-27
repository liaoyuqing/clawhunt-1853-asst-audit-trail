#!/usr/bin/env python
"""真实进程级端到端取证：起 uvicorn，用 httpx 打真实 HTTP，逐条跑 4 个验收场景。

与 pytest 的区别：pytest 用 TestClient（进程内 ASGI 调用）；本脚本起真实 uvicorn 进程、
走真实 socket 与 HTTP，并把每一次请求/响应写进 reports/e2e-evidence.md，作为「接口真的能跑」的独立证据。

用法：python scripts/e2e_evidence.py
输出：reports/e2e-evidence.md（同时打印到最后一行 EXIT=0/1）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "e2e-evidence.md"

ORG = uuid.uuid4()
OTHER_ORG = uuid.uuid4()
SITE = uuid.uuid4()
EMPLOYEE = uuid.uuid4()
AUDITOR = uuid.uuid4()
CONFIRMER = uuid.uuid4()

EMPLOYEE_TOKEN = "e2e-employee-token"
AUDITOR_TOKEN = "e2e-auditor-token"
OTHER_TOKEN = "e2e-other-org-auditor-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Transcript:
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures = 0

    def section(self, title: str) -> None:
        self.blocks.append(f"\n## {title}\n")

    def note(self, text: str) -> None:
        self.blocks.append(text)

    def call(self, label: str, response, *, expect: int | None = None) -> dict:
        body: object
        try:
            body = response.json()
        except Exception:
            body = response.text
        dumped = json.dumps(body, ensure_ascii=False, indent=2)
        if len(dumped) > 2600:
            dumped = dumped[:2600] + "\n... <truncated>"
        print(f"[e2e] {label} -> {response.status_code}", file=sys.stderr, flush=True)
        ok = expect is None or response.status_code == expect
        if not ok:
            self.failures += 1
        mark = "OK" if ok else "MISMATCH"
        self.blocks.append(
            f"### {label}\n\n"
            f"- 请求：`{response.request.method} {response.request.url.path}"
            + (f"?{response.request.url.query.decode()}" if response.request.url.query else "")
            + "`\n"
            f"- 状态码：**{response.status_code}**（期望 {expect}，{mark}）\n"
            f"- 响应：\n\n```json\n{dumped}\n```\n"
        )
        return body if isinstance(body, dict) else {"raw": body}


def wait_ready(base_url: str, timeout: float = 60.0) -> None:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/healthz", timeout=2.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("uvicorn 未在超时内就绪")


def main() -> int:
    import httpx

    workdir = Path(tempfile.mkdtemp(prefix="asst-e2e-"))
    db_path = workdir / "e2e.db"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    import base64

    env = dict(os.environ)
    env.update(
        {
            "ASST_DATABASE_URL": f"sqlite+pysqlite:///{db_path.as_posix()}",
            "ASST_TRAIL_KEY": base64.b64encode(os.urandom(32)).decode(),
            "ASST_EXPORT_DIR": str(workdir / "exports"),
            "ASST_EXPORT_QUEUE": "inline",
            "ASST_LOG_RETENTION_DAYS": "180",
            "ASST_PUBLIC_BASE_URL": base_url,
            "ASST_IAM_STUB_TOKENS": ";".join(
                [
                    f"{EMPLOYEE_TOKEN}:{EMPLOYEE}:{ORG}:employee:{SITE}",
                    f"{AUDITOR_TOKEN}:{AUDITOR}:{ORG}:auditor:",
                    f"{OTHER_TOKEN}:{AUDITOR}:{OTHER_ORG}:auditor:",
                ]
            ),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )

    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    server_lines: list[str] = []

    def _drain(stream) -> None:
        # 必须持续读走子进程 stdout：否则管道缓冲写满后 uvicorn 会阻塞在写日志上，整台服务卡死
        try:
            for line in stream:
                server_lines.append(line.rstrip("\n"))
        except Exception:  # noqa: BLE001 - 进程结束时的读错误可忽略
            pass

    import threading

    drain_thread = threading.Thread(target=_drain, args=(server.stdout,), daemon=True)
    drain_thread.start()

    tx = Transcript()
    tx.blocks.append(
        "# 端到端取证（真实 uvicorn 进程 + 真实 HTTP）\n\n"
        f"- 生成时间：{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        f"- 启动命令：`python -m uvicorn app.main:app --host 127.0.0.1 --port {port}`\n"
        "- 建表方式：`python -m alembic upgrade head`\n"
        "- 数据库：临时 SQLite（本脚本只做单机端到端取证；连接真实 PostgreSQL 15.19 / 真实 Redis 的验证见 reports/pg-migration.log、reports/pg-immutability.log、reports/pytest-pg.log、reports/redis-queue-e2e.log）\n"
        "- 身份来源：S2-IAM 桩（`ASST_IAM_STUB_TOKENS` 映射到虚构 UUID）\n"
    )

    try:
        wait_ready(base_url)
        client = httpx.Client(base_url=base_url, timeout=30.0)
        emp = {"Authorization": f"Bearer {EMPLOYEE_TOKEN}"}
        aud = {"Authorization": f"Bearer {AUDITOR_TOKEN}"}

        tx.section("环境自检")
        tx.call("GET /healthz", client.get("/healthz"), expect=200)

        # ---------- AC1 ----------
        tx.section("AC1 用户删除会话后，审计接口仍能查到该会话完整内容")
        conv = tx.call(
            "1-1 新建会话",
            client.post("/api/assistant/conversations", headers=emp, json={"title": "E2E 虚构会话", "site_id": str(SITE)}),
            expect=201,
        )["id"]
        tx.call(
            "1-2 写用户消息（原文加密 + 脱敏文本）",
            client.post(
                f"/api/assistant/conversations/{conv}/messages",
                headers=emp,
                json={"content": "帮我查会员 13812345678 的对接进度"},
            ),
            expect=201,
        )
        tx.call(
            "1-3 写回答（引用 doc_id 与版本、模型名、提示词版本）",
            client.post(
                f"/api/assistant/conversations/{conv}/answers",
                headers=emp,
                json={
                    "content": "按虚构制度 v3 第 4 条，该会员可对接。",
                    "doc_refs": [{"doc_id": "doc-0001", "doc_version": "v3", "title": "虚构制度"}],
                    "model_name": "stub-model-1",
                    "prompt_version": "p-2026-01",
                },
            ),
            expect=201,
        )
        tx.call(
            "1-4 写工具调用（脱敏参数 + 结果摘要 + 确认人与时间）",
            client.post(
                f"/api/assistant/conversations/{conv}/tool-calls",
                headers=emp,
                json={
                    "tool_name": "crm.member.lookup",
                    "tool_version": "1.4.2",
                    "params": {"phone": "13812345678", "email": "zhangsan@example.com"},
                    "result_summary": "命中 1 条虚构会员记录",
                    "status": "ok",
                    "confirmed_by": str(CONFIRMER),
                    "confirmed_at": "2026-11-20T02:00:00",
                },
            ),
            expect=201,
        )
        tx.call(
            "1-5 写关停开关拦截事件",
            client.post(
                f"/api/assistant/conversations/{conv}/blocked-events",
                headers=emp,
                json={
                    "block_reason": "kill_switch_on",
                    "rule_id": "asst.kill_switch.global",
                    "attempted_tool": "crm.member.export",
                    "detail": "关停开关处于开启状态，拒绝导出",
                },
            ),
            expect=201,
        )
        tx.call(
            "1-6 写用户反馈（差评）",
            client.post(
                f"/api/assistant/conversations/{conv}/feedback",
                headers=emp,
                json={"rating": 2, "comment": "回答里有 13812345678 这样的号码"},
            ),
            expect=201,
        )
        before = tx.call(
            "1-7 删除前：审计详情",
            client.get(f"/api/assistant/audit/conversations/{conv}", headers=aud),
            expect=200,
        )
        tx.call(
            "1-8 界面「删除会话」（只置 hidden_by_user，204）",
            client.delete(f"/api/assistant/conversations/{conv}", headers=emp),
            expect=204,
        )
        after = tx.call(
            "1-9 删除后：审计详情仍 200，条数不变，内容解密后完整",
            client.get(f"/api/assistant/audit/conversations/{conv}", headers=aud),
            expect=200,
        )
        same_count = before.get("trail_count") == after.get("trail_count")
        msg = [t for t in after.get("trails", []) if t["kind"] == "message"]
        content_ok = bool(msg) and msg[0]["content"] == "帮我查会员 13812345678 的对接进度"
        redacted_ok = bool(msg) and "138****5678" in (msg[0].get("content_redacted") or "")
        tx.note(
            f"\n**核对**：`hidden_by_user={after.get('hidden_by_user')}`，"
            f"删除前后 `trail_count` 均为 {after.get('trail_count')}（一致={same_count}），"
            f"解密原文逐字一致={content_ok}，脱敏文本掩码生效={redacted_ok}\n"
        )
        if not (same_count and content_ok and redacted_ok and after.get("hidden_by_user") is True):
            tx.failures += 1

        # ---------- AC2 ----------
        tx.section("AC2 篡改一条历史留痕后，哈希链校验任务报告断点位置")
        past_day = (date.today() - timedelta(days=3)).isoformat()
        for index in range(5):
            client.post(
                f"/api/assistant/conversations/{conv}/messages",
                headers=emp,
                json={"content": f"链测试消息 {index}", "occurred_at": f"{past_day}T10:0{index}:00"},
            )
        tx.call(
            "2-1 计算指定日期的哈希链",
            client.post("/api/assistant/ops/hash-chain/build", headers=emp, json={"chain_date": past_day}),
            expect=200,
        )
        tx.call(
            "2-2 未篡改时校验（应 ok）",
            client.post("/api/assistant/ops/hash-chain/verify", headers=emp, json={"chain_date": past_day}),
            expect=200,
        )
        tamper_ok = tamper_and_verify(env, workdir, int(chain_seq_of(past_day)), tx, client, emp, past_day)
        if not tamper_ok:
            tx.failures += 1

        # ---------- AC3 ----------
        tx.section("AC3 非审计角色 403；审计角色每次查看产生一条审计记录")
        tx.call(
            "3-1 员工角色查审计列表（期望 403）",
            client.get("/api/assistant/audit/conversations", headers=emp),
            expect=403,
        )
        tx.call(
            "3-2 员工角色查审计详情（期望 403）",
            client.get(f"/api/assistant/audit/conversations/{conv}", headers=emp),
            expect=403,
        )
        tx.call(
            "3-3 员工角色建导出（期望 403）",
            client.post("/api/assistant/audit/exports", headers=emp, json={}),
            expect=403,
        )
        tx.call(
            "3-4 无 Authorization 头（期望 401）",
            client.get("/api/assistant/audit/conversations"),
            expect=401,
        )
        tx.call(
            "3-5 只给 X-Api-Key（期望 401，只认 Bearer）",
            client.get("/api/assistant/audit/conversations", headers={"X-Api-Key": AUDITOR_TOKEN}),
            expect=401,
        )
        view_baseline = count_audit(env, workdir, "audit_view")
        for index in range(3):
            tx.call(
                f"3-6 审计角色第 {index + 1} 次查列表（期望 200）",
                client.get(
                    "/api/assistant/audit/conversations",
                    headers=aud,
                    params={"page_size": 10, "tool_name": "crm.member.lookup"},
                ),
                expect=200,
            )
        view_after = count_audit(env, workdir, "audit_view")
        tx.note(
            f"\n**核对**：查看前 `audit_view` = {view_baseline}，连查 3 次后 = {view_after}，"
            f"增量 = {view_after - view_baseline}（期望 3）\n"
        )
        if view_after - view_baseline != 3:
            tx.failures += 1
        tx.call(
            "3-7 跨 org 的审计角色查本 org 会话（期望 404，防枚举）",
            client.get(
                f"/api/assistant/audit/conversations/{conv}",
                headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
            ),
            expect=404,
        )

        # ---------- AC4 ----------
        tx.section("AC4 到期数据清理后，审计日志中有删除摘要")
        old_day = (date.today() - timedelta(days=200)).isoformat()
        for index in range(4):
            client.post(
                f"/api/assistant/conversations/{conv}/messages",
                headers=emp,
                json={"content": f"200 天前的历史消息 {index}", "occurred_at": f"{old_day}T09:0{index}:00"},
            )
        purge = tx.call(
            "4-1 触发保留期清理（默认 180 天）",
            client.post("/api/assistant/ops/retention/purge", headers=emp, json={}),
            expect=200,
        )
        tx.call(
            "4-2 清理后再查审计详情（近期留痕仍在）",
            client.get(f"/api/assistant/audit/conversations/{conv}", headers=aud),
            expect=200,
        )
        purge_rows = query_audit_detail(env, workdir, "purge")
        tx.note(
            "\n**核对**：清理摘要 = "
            + json.dumps(purge, ensure_ascii=False)
            + "\n\n`audit_log` 里的删除摘要：\n\n```json\n"
            + json.dumps(purge_rows, ensure_ascii=False, indent=2)
            + "\n```\n"
        )
        ok4 = (
            purge_rows
            and purge_rows[0].get("count") == 4
            and purge_rows[0].get("range_from")
            and purge_rows[0].get("range_to")
            and purge_rows[0].get("retention_days") == 180
        )
        if not ok4:
            tx.failures += 1
        tx.note(f"\n**核对结论**：删除摘要四要素（count / range_from / range_to / retention_days）齐全={bool(ok4)}\n")

        # ---------- 导出 ----------
        tx.section("附加：异步 JSONL 导出 + 10 分钟签名下载链接")
        job = tx.call(
            "5-1 POST /api/assistant/audit/exports（202 + job_id）",
            client.post("/api/assistant/audit/exports", headers=aud, json={}),
            expect=202,
        )
        status = tx.call(
            "5-2 GET 任务状态（finished + download_url）",
            client.get(f"/api/assistant/audit/exports/{job['job_id']}", headers=aud),
            expect=200,
        )
        url = status.get("download_url") or ""
        path = url.replace(base_url, "")
        download = client.get(path)
        lines_out = download.text.splitlines()
        tx.note(
            f"\n**下载**：`GET {path.split('&sig=')[0]}&sig=<redacted>` → **{download.status_code}**，"
            f"共 {len(lines_out)} 行（1 行 manifest + {len(lines_out) - 1} 条留痕），"
            f"首行类型 = `{json.loads(lines_out[0])['type'] if lines_out else 'n/a'}`\n"
        )
        if download.status_code != 200 or len(lines_out) < 2:
            tx.failures += 1
        second = client.get(path)
        tx.note(f"**单次可用**：同一链接第二次下载 → **{second.status_code}**（期望 410）\n")
        if second.status_code != 410:
            tx.failures += 1

        tx.section("结论")
        tx.note(
            f"- 失败项计数：**{tx.failures}**\n"
            f"- 判定：{'全部符合预期' if tx.failures == 0 else '存在不符项，见上方 MISMATCH'}\n"
        )

    except Exception as exc:  # noqa: BLE001 - 失败时把服务端日志一并带出来
        import traceback
        print(f"[e2e] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        tx.failures += 1
        tx.section("异常中止")
        tx.note(f"- 异常：`{type(exc).__name__}: {exc}`")
        try:
            server.terminate()
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        except Exception:  # noqa: BLE001
            pass
        drain_thread.join(timeout=5)
        server_out = "\n".join(server_lines)
        print("--- server output tail ---", file=sys.stderr)
        print(server_out[-4000:], file=sys.stderr)
        tx.note("- 服务端日志尾部：" + chr(10) + chr(10) + "```" + chr(10) + server_out[-2000:] + chr(10) + "```")
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        if server_lines:
            tx.blocks.append(
                "\n## 服务端访问日志（uvicorn）\n\n```\n"
                + "\n".join(server_lines[-60:])
                + "\n```\n"
            )
        drain_thread.join(timeout=5)
        shutil.rmtree(workdir, ignore_errors=True)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(tx.blocks) + "\n", encoding="utf-8")
    print(f"report -> {REPORT}")
    print(f"EXIT={0 if tx.failures == 0 else 1}")
    return 0 if tx.failures == 0 else 1


def _sqlite(db_path: Path, sql: str, params: tuple = ()):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _db_file(env: dict) -> Path:
    return Path(env["ASST_DATABASE_URL"].split("///", 1)[1])


def count_audit(env: dict, workdir: Path, action: str) -> int:
    rows = _sqlite(_db_file(env), "SELECT count(*) FROM audit_log WHERE action = ?", (action,))
    return int(rows[0][0])


def query_audit_detail(env: dict, workdir: Path, action: str) -> list[dict]:
    rows = _sqlite(
        _db_file(env),
        "SELECT detail FROM audit_log WHERE action = ? ORDER BY created_at DESC LIMIT 1",
        (action,),
    )
    return [json.loads(row[0]) for row in rows]


def chain_seq_of(day: str) -> int:
    return 0


def tamper_and_verify(env, workdir, _unused, tx, client, emp, day) -> bool:
    """模拟「数据库超级用户绕过应用层」：直接改 payload，再走校验接口看断点。"""
    db = _db_file(env)
    target = _sqlite(db, "SELECT seq FROM asst_trail WHERE chain_date = ? ORDER BY seq LIMIT 1 OFFSET 2", (day,))
    if not target:
        tx.note("\n**篡改步骤**：该日留痕不足 3 条，无法按第 3 条篡改\n")
        return False
    seq = int(target[0][0])
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_asst_trail_no_update")
        conn.execute(
            "UPDATE asst_trail SET payload = json_set(payload, '$.content_redacted', 'TAMPERED-BY-E2E') "
            "WHERE seq = ?",
            (seq,),
        )
        conn.commit()
    finally:
        conn.close()
    tx.note(
        f"\n**篡改步骤**：先 `DROP TRIGGER trg_asst_trail_no_update`（模拟超级用户绕过），"
        f"再 `UPDATE asst_trail SET payload = json_set(payload, '$.content_redacted', 'TAMPERED-BY-E2E') WHERE seq = {seq}`\n"
    )
    verify = tx.call(
        "2-3 篡改后校验（应报断点）",
        client.post("/api/assistant/ops/hash-chain/verify", headers=emp, json={"chain_date": day}),
        expect=200,
    )
    ok = (
        verify.get("ok") is False
        and verify.get("first_break_seq") is not None
        and verify.get("expected_hash")
        and verify.get("actual_hash")
        and verify.get("expected_hash") != verify.get("actual_hash")
    )
    tx.note(
        f"\n**核对**：`ok={verify.get('ok')}`，`first_break_seq={verify.get('first_break_seq')}`，"
        f"`break_reason={verify.get('break_reason')}`，"
        f"expected≠actual = {verify.get('expected_hash') != verify.get('actual_hash')}\n"
    )
    return ok


if __name__ == "__main__":
    raise SystemExit(main())