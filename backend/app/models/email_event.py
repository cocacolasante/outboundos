from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tenancy.mixin import TenantMixin

if TYPE_CHECKING:
    from app.models.campaign import Campaign
    from app.models.lead import Lead


class EmailEventType(str, enum.Enum):
    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    SOFT_BOUNCE = "soft_bounce"
    HARD_BOUNCE = "hard_bounce"
    SPAM = "spam"
    UNSUBSCRIBED = "unsubscribed"
    REPLIED = "replied"
    # Brevo refused the send because the recipient is blocklisted/blocked.
    BLOCKED = "blocked"


class EmailEvent(TenantMixin, Base):
    __tablename__ = "email_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[EmailEventType] = mapped_column(
        Enum(EmailEventType, name="email_event_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    event_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    lead: Mapped["Lead"] = relationship(back_populates="events")
    campaign: Mapped["Campaign"] = relationship(back_populates="events")
