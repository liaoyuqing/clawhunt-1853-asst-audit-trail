"""会话表：删除会话只是 hidden_by_user=True，留痕行不受影响。"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BusinessColumnsMixin, UUIDPrimaryKeyMixin


class AsstConversation(UUIDPrimaryKeyMixin, BusinessColumnsMixin, Base):
    __tablename__ = "asst_conversation"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    hidden_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_asst_conversation_org_user", "org_id", "user_id", "created_at"),
    )
