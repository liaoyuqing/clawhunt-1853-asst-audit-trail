"""FastAPI 依赖：DB 会话、S2-IAM 主体、审计角色闸门、加密器、配置。

审计角色闸门（AC3）：所有 /api/assistant/audit/* 路由都挂 require_auditor，
非审计角色 → 403 FORBIDDEN_AUDIT_ROLE；未认证 → 401。
"""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.services.crypto import TrailCipher
from app.db.session import build_engine, build_session_factory
from app.services.iam import (
    ForbiddenRole,
    NotAuthenticated,
    Principal,
    StubIamClient,
)

_settings = load_settings()
_engine = build_engine(_settings)
_session_factory = build_session_factory(_engine)
_iam = StubIamClient()


def get_engine():
    return _engine


def get_session() -> Iterator[Session]:
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_settings() -> Settings:
    return _settings


def get_cipher(settings: Settings = Depends(get_settings)) -> TrailCipher:
    return TrailCipher.from_env_value(settings.trail_key)


def get_iam() -> StubIamClient:
    return _iam


def _bearer(authorization: str | None) -> str | None:
    """只接受 Authorization: Bearer <token>（X-Api-Key 等其它头一律不认）。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    iam: StubIamClient = Depends(get_iam),
) -> Principal:
    try:
        return iam.resolve_principal(_bearer(authorization))
    except NotAuthenticated as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_auditor(principal: Principal = Depends(get_principal)) -> Principal:
    """审计接口的角色闸门：非审计角色 403。"""
    from fastapi import HTTPException

    try:
        _iam.assert_audit_access(principal)
    except ForbiddenRole as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": exc.code, "message": str(exc), "role": sorted(principal.roles)},
        ) from exc
    return principal


def client_ip(request: Request) -> str | None:
    """审计日志记 IP：优先反代透传头，其次直连地址。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
