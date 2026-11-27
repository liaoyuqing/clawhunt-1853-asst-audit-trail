"""S2-IAM 对接点：身份、角色与数据范围。

本单自带一个桩（StubIamClient），把 S2-IAM 的契约用最小实现表达清楚：
  * resolve_principal(bearer_token) -> Principal(operator_id, org_id, site_ids, roles)
  * assert_audit_access(principal) -> 审计角色校验（失败 403）
  * apply_site_scope(stmt) -> 按 site 数据范围收窄查询

真实环境把这三点换成 S2-IAM 的 SDK / HTTP 调用即可，调用方（app/api/deps.py）不变。
README 有逐字段对接表与「哪些是桩、哪些不需后端」的说明。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

ROLE_EMPLOYEE = "employee"
ROLE_AUDITOR = "auditor"
ROLE_ADMIN = "admin"

AUDIT_ROLES = frozenset({ROLE_AUDITOR})


class IamError(RuntimeError):
    """S2-IAM 不可用或返回非法数据。"""


class NotAuthenticated(RuntimeError):
    """缺少或无效的 Bearer 凭据 → 401。"""


class ForbiddenRole(RuntimeError):
    """身份有效但角色无权限 → 403，错误码 FORBIDDEN_AUDIT_ROLE。"""

    code = "FORBIDDEN_AUDIT_ROLE"


@dataclass(frozen=True)
class Principal:
    """已认证主体：对应 S2-IAM 的 token introspection 结果。"""

    operator_id: uuid.UUID
    org_id: uuid.UUID
    roles: frozenset[str] = frozenset()
    site_ids: tuple[uuid.UUID, ...] = ()
    display_name: str = ""

    @property
    def is_auditor(self) -> bool:
        return bool(self.roles & AUDIT_ROLES)

    @property
    def scoped_to_sites(self) -> bool:
        """site_ids 为空表示全组织不限；否则只允许看这些 site。"""
        return bool(self.site_ids)


def parse_role_list(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


class StubIamClient:
    """S2-IAM 的桩实现：把 Bearer 令牌映射成 Principal。

    映射格式（本地联调与测试用，生产换成真实 S2-IAM）：
      ASST_IAM_STUB_TOKENS=tok:<operator_uuid>:<org_uuid>:<role,role>:<site_uuid|site_uuid>;...

    令牌本身不落日志、不回显；映射里只用虚构的测试身份。
    """

    def __init__(self, mapping: dict[str, Principal] | None = None) -> None:
        self._mapping = dict(mapping) if mapping is not None else _parse_env_mapping()

    def resolve_principal(self, bearer_token: str | None) -> Principal:
        """把 Bearer 令牌换成 Principal；未知令牌 → 401。"""
        if not bearer_token:
            raise NotAuthenticated("缺少 Authorization: Bearer <token>")
        principal = self._mapping.get(bearer_token)
        if principal is None:
            raise NotAuthenticated("Bearer 令牌无效或已过期")
        return principal

    def assert_audit_access(self, principal: Principal) -> None:
        """审计接口只允许审计角色；其余角色一律 403。"""
        if not principal.is_auditor:
            raise ForbiddenRole(
                "audit endpoints require the auditor role; current roles: "
                + (", ".join(sorted(principal.roles)) or "<none>")
            )


def _parse_env_mapping() -> dict[str, Principal]:
    raw = os.environ.get("ASST_IAM_STUB_TOKENS", "")
    mapping: dict[str, Principal] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 3:
            continue
        token, operator, org = parts[0], parts[1], parts[2]
        roles = parse_role_list(parts[3] if len(parts) > 3 else "")
        sites = tuple(
            uuid.UUID(s) for s in (parts[4].split("|") if len(parts) > 4 and parts[4] else [])
        )
        mapping[token] = Principal(
            operator_id=uuid.UUID(operator),
            org_id=uuid.UUID(org),
            roles=roles,
            site_ids=sites,
        )
    return mapping
