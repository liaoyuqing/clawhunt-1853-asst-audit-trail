"""留痕写入：5 类事件（消息 / 回答 / 工具调用 / 拦截事件 / 用户反馈）。

只提供 INSERT 路径：本模块**没有任何** UPDATE / DELETE 会话或留痕行的代码；
「删除会话」由 app/services/conversations.py 实现为置 hidden_by_user。

每条留痕同时写：
  * content_enc —— AES-256-GCM 密文（原文，永不落明文）
  * content_redacted —— 脱敏文本（列表与导出默认展示）
  * content_sha256 —— 原文指纹（供详情接口的完整性自证与离线复核）
  * payload —— 参与哈希链的规范化快照（写入时冻结）
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.canonical import canonical_json, normalize
from app.services.crypto import TrailCipher, sha256_hex
from app.services.crypto import redact_text, redact_value
from app.db.base import utcnow
from app.models.asst_conversation import AsstConversation
from app.models.asst_trail import (
    TRAIL_KIND_ANSWER,
    TRAIL_KIND_BLOCKED,
    TRAIL_KIND_FEEDBACK,
    TRAIL_KIND_MESSAGE,
    TRAIL_KIND_TOOL_CALL,
    AsstTrail,
    AsstTrailCounter,
)

PAYLOAD_SCHEMA_VERSION = 1


class TrailWriter:
    """把助手会话里的五类事件写成不可变留痕。

    用法：构造时给会话（commit 由调用方控制），每条事件调用对应方法，返回落库后的 AsstTrail。
    """

    def __init__(self, session: Session, cipher: TrailCipher) -> None:
        self.session = session
        self.cipher = cipher

    # ---------- 5 类写入 ----------

    def record_message(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        content: str,
        occurred_at: datetime | None = None,
    ) -> AsstTrail:
        """用户消息：原文加密 + 脱敏文本。"""
        return self._append(
            conversation=conversation,
            user_id=user_id,
            kind=TRAIL_KIND_MESSAGE,
            occurred_at=occurred_at,
            content=content,
            payload_extra={"kind": TRAIL_KIND_MESSAGE, "role": "user", "chars": len(content)},
        )

    def record_answer(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        content: str,
        doc_refs: Sequence[dict[str, Any]],
        model_name: str,
        prompt_version: str,
        occurred_at: datetime | None = None,
    ) -> AsstTrail:
        """助手回答：正文加密 + 引用的 doc_id 与版本 + 模型名 + 提示词版本。"""
        refs = [
            {
                "doc_id": str(ref.get("doc_id", "")),
                "doc_version": str(ref.get("doc_version", "")),
                "title": str(ref.get("title", "")),
            }
            for ref in doc_refs
        ]
        return self._append(
            conversation=conversation,
            user_id=user_id,
            kind=TRAIL_KIND_ANSWER,
            occurred_at=occurred_at,
            content=content,
            model_name=model_name,
            prompt_version=prompt_version,
            doc_refs=refs,
            payload_extra={
                "kind": TRAIL_KIND_ANSWER,
                "role": "assistant",
                "model_name": model_name,
                "prompt_version": prompt_version,
                "doc_refs": refs,
            },
        )

    def record_tool_call(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        tool_name: str,
        tool_version: str,
        params: dict[str, Any],
        result_summary: str,
        status: str = "ok",
        confirmed_by: uuid.UUID | None = None,
        confirmed_at: datetime | None = None,
        occurred_at: datetime | None = None,
    ) -> AsstTrail:
        """工具调用：参数先脱敏再落库（payload 与展示列都用脱敏值）。"""
        params_redacted = redact_value(dict(params))
        summary_redacted = redact_text(result_summary)
        return self._append(
            conversation=conversation,
            user_id=user_id,
            kind=TRAIL_KIND_TOOL_CALL,
            occurred_at=occurred_at,
            tool_name=tool_name,
            tool_version=tool_version,
            params_redacted=params_redacted,
            result_summary=summary_redacted,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            status=status,
            payload_extra={
                "kind": TRAIL_KIND_TOOL_CALL,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "params_redacted": params_redacted,
                "result_summary": summary_redacted,
                "status": status,
                "confirmed_by": confirmed_by,
                "confirmed_at": confirmed_at,
            },
        )

    def record_blocked_event(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        block_reason: str,
        rule_id: str,
        attempted_tool: str | None = None,
        detail: str | None = None,
        occurred_at: datetime | None = None,
    ) -> AsstTrail:
        """关停开关拦截事件：记拦截原因与命中规则，便于事后追责与规则调优。"""
        detail_redacted = redact_text(detail)
        return self._append(
            conversation=conversation,
            user_id=user_id,
            kind=TRAIL_KIND_BLOCKED,
            occurred_at=occurred_at,
            block_reason=block_reason,
            rule_id=rule_id,
            result_summary=detail_redacted,
            payload_extra={
                "kind": TRAIL_KIND_BLOCKED,
                "block_reason": block_reason,
                "rule_id": rule_id,
                "attempted_tool": attempted_tool,
                "detail": detail_redacted,
            },
        )

    def record_feedback(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        rating: int,
        comment: str | None = None,
        related_trail_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> AsstTrail:
        """用户反馈：rating（1-5）+ 脱敏评论；差评即 rating <= 2。"""
        if not 1 <= rating <= 5:
            raise ValueError("rating 必须在 1..5 之间")
        comment_redacted = redact_text(comment)
        return self._append(
            conversation=conversation,
            user_id=user_id,
            kind=TRAIL_KIND_FEEDBACK,
            occurred_at=occurred_at,
            rating=rating,
            comment_redacted=comment_redacted,
            related_trail_id=related_trail_id,
            payload_extra={"kind": TRAIL_KIND_FEEDBACK, "rating": rating, "comment": comment_redacted},
        )

    # ---------- 内部 ----------

    def _next_seq(self, org_id: uuid.UUID) -> int:
        """原子递增 org 内序号：先 UPDATE ... RETURNING，无行再 INSERT 初始行。"""
        stmt = (
            update(AsstTrailCounter)
            .where(AsstTrailCounter.org_id == org_id)
            .values(last_value=AsstTrailCounter.last_value + 1, updated_at=utcnow())
            .returning(AsstTrailCounter.last_value)
        )
        value = self.session.execute(stmt).scalar_one_or_none()
        if value is not None:
            return int(value)
        self.session.add(AsstTrailCounter(org_id=org_id, last_value=1))
        try:
            self.session.flush()
        except IntegrityError:
            # 并发下别的请求先插入了计数器行，重试一次 UPDATE 即可
            self.session.rollback()
            value = self.session.execute(stmt).scalar_one()
            return int(value)
        return 1

    def _append(
        self,
        *,
        conversation: AsstConversation,
        user_id: uuid.UUID,
        kind: str,
        payload_extra: dict[str, Any],
        occurred_at: datetime | None = None,
        content: str | None = None,
        **columns: Any,
    ) -> AsstTrail:
        occurred = occurred_at or utcnow()
        seq = self._next_seq(conversation.org_id)
        chain_date = occurred.date() if isinstance(occurred, datetime) else occurred

        trail_id = uuid.uuid4()
        aad = trail_aad(trail_id)
        content_enc = None
        content_redacted = None
        content_digest = None
        if content is not None:
            content_enc = self.cipher.encrypt(content, aad=aad)
            content_redacted = redact_text(content)
            content_digest = sha256_hex(content.encode("utf-8"))

        trail = AsstTrail(
            id=trail_id,
            org_id=conversation.org_id,
            site_id=conversation.site_id,
            conversation_id=conversation.id,
            user_id=user_id,
            seq=seq,
            chain_date=chain_date,
            occurred_at=occurred,
            kind=kind,
            content_enc=content_enc,
            content_redacted=content_redacted,
            content_sha256=content_digest,
            payload=self._build_payload(
                seq=seq,
                org_id=conversation.org_id,
                site_id=conversation.site_id,
                conversation_id=conversation.id,
                user_id=user_id,
                occurred_at=occurred,
                content_digest=content_digest,
                content_redacted=content_redacted,
                payload_extra=payload_extra,
            ),
            hidden_by_user=conversation.hidden_by_user,
            created_by=user_id,
            **columns,
        )
        self.session.add(trail)
        self.session.flush()
        if conversation.hidden_by_user:
            # 会话已隐藏时新写入的留痕也带隐藏标记，导出与筛选口径保持一致
            trail.hidden_by_user = True
        return trail

    @staticmethod
    def _build_payload(
        *,
        seq: int,
        org_id: uuid.UUID,
        site_id: uuid.UUID | None,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        occurred_at: datetime,
        content_digest: str | None,
        content_redacted: str | None,
        payload_extra: dict[str, Any],
    ) -> dict[str, Any]:
        """规范化 payload：哈希链的输入。字段顺序由 canonical_json 排序保证。"""
        payload: dict[str, Any] = {
            "schema": PAYLOAD_SCHEMA_VERSION,
            "seq": seq,
            "org_id": str(org_id),
            "site_id": str(site_id) if site_id else None,
            "conversation_id": str(conversation_id),
            "user_id": str(user_id),
            "occurred_at": occurred_at,
            "content_sha256": content_digest,
            "content_redacted": content_redacted,
        }
        payload.update(payload_extra)
        # 归一化后再落 JSON 列：datetime/UUID 一律转成确定性字面量，
        # 否则 JSON 列序列化失败，且哈希会随进程/时区漂移
        return normalize(payload)


def trail_aad(trail_id: uuid.UUID | str) -> bytes:
    """GCM 附加认证数据：把密文绑到留痕行 id，防止整段密文被搬到别的行。"""
    return f"asst_trail:{trail_id}".encode("utf-8")


def payload_digest(payload: dict[str, Any]) -> str:
    """payload 的 sha256：链任务与校验任务共用同一个口径。"""
    return sha256_hex(canonical_json(payload).encode("utf-8"))