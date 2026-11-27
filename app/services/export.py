"""异步 JSONL 导出 + 10 分钟有效下载链接。

流程：POST /audit/exports → 建 AsstExportJob(queued) → 202 + job_id；
      worker 按同一套筛选条件逐行写 JSONL 落盘 → status=finished；
      GET /audit/exports/{job_id} → status / row_count / download_url；
      download_url 带 HMAC 签名，10 分钟有效、单次可用（重复使用返回 410）。

队列：ASST_EXPORT_QUEUE=inline（测试同步跑）/ thread（进程内线程）。
真实环境换成 Redis 队列（requirements 里的 redis 即为这条链路预留），接口不变。
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.core.canonical import canonical_json
from app.services.crypto import DecryptionError, TrailCipher, derive_signing_key, sha256_hex
from app.db.base import utcnow
from app.models.asst_export_job import AsstExportJob, AsstExportToken
from app.models.asst_hash_chain import AsstHashChainEntry
from app.services.query import TrailFilter, query_trails
from app.services.trail_writer import trail_aad

EXPORT_FORMAT = "jsonl"


@dataclass
class ExportResult:
    job_id: str
    status: str
    row_count: int
    file_path: str | None
    file_sha256: str | None
    download_url: str | None
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "format": EXPORT_FORMAT,
            "row_count": self.row_count,
            "file_path": self.file_path,
            "file_sha256": self.file_sha256,
            "download_url": self.download_url,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def export_dir(settings: Settings) -> Path:
    path = Path(settings.export_dir).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sign(secret: bytes, token: str) -> str:
    return hmac.new(secret, token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_export_job(
    session: Session,
    *,
    org_id: uuid.UUID,
    requested_by: uuid.UUID,
    filters: TrailFilter,
    site_id: uuid.UUID | None = None,
) -> AsstExportJob:
    job = AsstExportJob(
        org_id=org_id,
        site_id=site_id,
        requested_by=requested_by,
        status="queued",
        filters=filters.as_dict(),
        format=EXPORT_FORMAT,
        created_by=requested_by,
    )
    session.add(job)
    session.flush()
    return job


def run_export_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    cipher: TrailCipher,
    settings: Settings,
) -> AsstExportJob:
    """执行导出：首行 manifest + 每行一条留痕；内容为解密后原文，附脱敏文本与指纹。"""
    job = session.get(AsstExportJob, job_id)
    if job is None:
        raise LookupError("导出任务不存在")
    job.status = "running"
    job.started_at = utcnow()
    session.flush()

    filters = TrailFilter.from_dict(job.filters)
    trails = query_trails(
        session, org_id=job.org_id, filters=filters, limit=settings.export_max_rows
    )
    target = export_dir(settings) / f"asst-audit-{job.id}.{EXPORT_FORMAT}"
    # 已建链的留痕带上 row_hash，供离线逐行复核；尚未建链的写 null（manifest 里说明）
    chain_rows = session.execute(
        select(AsstHashChainEntry.trail_id, AsstHashChainEntry.row_hash).where(
            AsstHashChainEntry.org_id == job.org_id
        )
    ).all()
    chain_hashes = {str(trail_id): row_hash for trail_id, row_hash in chain_rows}

    digest = hashlib.sha256()
    written = 0
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        header = {
            "type": "manifest",
            "job_id": str(job.id),
            "org_id": str(job.org_id),
            "format": EXPORT_FORMAT,
            "filters": job.filters,
            "generated_at": utcnow().isoformat(timespec="seconds") + "Z",
            "schema": 1,
            "row_hash_note": "row_hash 为留痕已建哈希链时的链上值；尚未建链的行为 null",
        }
        line = canonical_json(header)
        handle.write(line + "\n")
        digest.update((line + "\n").encode("utf-8"))
        for trail in trails:
            content = None
            if trail.content_enc:
                try:
                    content = cipher.decrypt(trail.content_enc, aad=trail_aad(trail.id))
                except DecryptionError:
                    content = None
            record = {
                "type": "trail",
                "seq": trail.seq,
                "trail_id": str(trail.id),
                "conversation_id": str(trail.conversation_id),
                "user_id": str(trail.user_id),
                "kind": trail.kind,
                "occurred_at": trail.occurred_at.isoformat(timespec="seconds") + "Z",
                "content": content,
                "content_redacted": trail.content_redacted,
                "content_sha256": trail.content_sha256,
                "result_digest": trail.result_digest,
                "payload": trail.payload,
                "row_hash": chain_hashes.get(str(trail.id)),
            }
            line = canonical_json(record)
            handle.write(line + "\n")
            digest.update((line + "\n").encode("utf-8"))
            written += 1

    job.status = "finished"
    job.row_count = written
    job.file_path = str(target)
    job.file_sha256 = digest.hexdigest()
    job.finished_at = utcnow()
    session.flush()
    return job


def issue_download_token(
    session: Session, *, job: AsstExportJob, settings: Settings
) -> tuple[str, datetime]:
    """签发下载令牌：库里只存 sha256，明文令牌只出现在返回给调用方的 URL 里。"""
    if not settings.trail_key:
        raise RuntimeError("缺少 ASST_TRAIL_KEY，无法派生下载签名密钥")
    secret = derive_signing_key(settings.trail_key, settings.export_signing_key)
    nonce = uuid.uuid4().hex
    raw = f"{job.id}.{nonce}"
    token_hash = sha256_hex(raw.encode("utf-8"))
    expires_at = utcnow() + timedelta(seconds=settings.export_url_ttl_seconds)
    session.add(
        AsstExportToken(
            job_id=job.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )
    session.flush()
    url = (
        f"{settings.public_base_url}/api/assistant/audit/exports/{job.id}/download"
        f"?token={raw}&sig={_sign(secret, raw)}"
    )
    return url, expires_at


def resolve_download(
    session: Session, *, job_id: uuid.UUID, raw_token: str, sig: str, settings: Settings
) -> Path:
    """校验签名 + 有效期 + 单次使用；返回可读文件路径。"""
    secret = derive_signing_key(settings.trail_key or "", settings.export_signing_key)
    if not hmac.compare_digest(_sign(secret, raw_token), sig):
        raise PermissionError("下载链接签名无效")
    if not raw_token.startswith(str(job_id) + "."):
        raise PermissionError("下载令牌与任务不匹配")
    token_hash = sha256_hex(raw_token.encode("utf-8"))
    token = session.execute(
        select(AsstExportToken).where(AsstExportToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if token is None or token.job_id != job_id:
        raise PermissionError("下载令牌不存在")
    if token.used_at is not None:
        raise FileExistsError("下载链接已使用过（单次有效）")
    if token.expires_at <= utcnow():
        raise TimeoutError("下载链接已过期（有效期 10 分钟）")
    job = session.get(AsstExportJob, job_id)
    if job is None or not job.file_path:
        raise LookupError("导出文件不存在")
    token.used_at = utcnow()
    session.flush()
    return Path(job.file_path)


def enqueue_export(
    *, session: Session, job_id: uuid.UUID, cipher: TrailCipher, settings: Settings
) -> None:
    """按 ASST_EXPORT_QUEUE 决定同步执行还是丢到进程内线程。"""
    if settings.export_queue == "inline":
        run_export_job(session, job_id=job_id, cipher=cipher, settings=settings)
        return

    def _worker() -> None:
        from app.db.session import build_engine, build_session_factory

        settings_local = load_settings()
        engine = build_engine(settings_local)
        factory = build_session_factory(engine)
        with factory() as bg:
            try:
                run_export_job(bg, job_id=job_id, cipher=cipher, settings=settings_local)
                bg.commit()
            except Exception:  # noqa: BLE001 - 后台任务失败要落库而不是静默
                bg.rollback()
                job = bg.get(AsstExportJob, job_id)
                if job is not None:
                    job.status = "failed"
                    bg.commit()

    threading.Thread(target=_worker, name=f"asst-export-{job_id}", daemon=True).start()