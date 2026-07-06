"""Agent models: runtime settings, notifications, and the audit log.

The CRM/inbox agent automates the boring half of CRM hygiene — logging
inbound replies, nudging the operator about due tasks and stale deals,
and (optionally) drafting suggested replies.  Its autonomy boundary is
deliberate: the agent may log activities, create reminder TASKS, and
email the OWNER; it may never convert a lead, message a prospect,
change an opportunity stage, or delete anything.  Those stay human
actions surfaced through the notifications below.

Three tables:

  AgentSettings  — single-row runtime config so the operator can toggle
                   behaviour from the UI without a redeploy.  The env
                   var ``AGENT_ENABLED`` remains the hard kill-switch
                   that wins over everything here.
  Notification   — persisted alert feed (the UI bell).  The email to
                   ``OWNER_NOTIFY_EMAIL`` is a side-channel; the row is
                   the source of truth.  ``dedup_key`` is UNIQUE so a
                   re-poll / re-sweep can never double-notify.
  AgentAction    — append-only audit of every autonomous decision
                   (including skips and failures) for transparency.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.tenancy.mixin import TenantMixin


class NotificationKind(str, enum.Enum):
    POSITIVE_REPLY = "positive_reply"
    REPLY = "reply"
    TASK_DUE = "task_due"
    TASK_OVERDUE = "task_overdue"
    STALE_OPPORTUNITY = "stale_opportunity"
    DIGEST = "digest"
    AGENT_ERROR = "agent_error"
    # Deliverability circuit breaker (migration 0028).
    CAMPAIGN_AUTO_PAUSED = "campaign_auto_paused"
    # Intent/trigger prospecting (migration 0030).
    PROSPECT_SIGNAL = "prospect_signal"
    # ICP lookalike discovery (migration 0031).
    LOOKALIKE_BATCH = "lookalike_batch"


class AgentActionType(str, enum.Enum):
    CLASSIFY_REPLY = "classify_reply"
    LOG_ACTIVITY = "log_activity"
    CREATE_REMINDER = "create_reminder"
    SEND_NOTIFICATION = "send_notification"
    DRAFT_REPLY = "draft_reply"
    FLAG_STALE_OPP = "flag_stale_opp"
    DIGEST = "digest"


class AgentActionStatus(str, enum.Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


class AgentSettings(TenantMixin, Base):
    """Per-tenant runtime agent config (multi-tenancy Phase 2 — was the
    app-wide int-PK singleton id=1).

    One row per tenant, created lazily by
    ``services.agent_core.get_agent_settings`` on first read so a fresh
    tenant needs no seed step.  NULLS NOT DISTINCT keeps the legacy
    no-tenant row unique too (workers run without tenant context until
    the worker-context phase).
    """

    __tablename__ = "agent_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_agent_settings_tenant",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )

    # Where this tenant's agent alerts + daily digest are emailed
    # (absorbs the global OWNER_NOTIFY_EMAIL/OWNER_NOTIFY_NAME env vars;
    # wired to the notification paths in the worker-context phase —
    # until then the env vars still drive email delivery).
    notify_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    notify_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Autonomy toggles — each one gates a specific agent behaviour.
    auto_log_replies: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    auto_create_convert_reminders: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    auto_draft_replies: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    stale_opp_nudges_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    daily_digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    notify_on_positive_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    notify_on_any_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )

    # Classifications below this confidence are logged but never acted
    # on (no reminder, no notification beyond the audit row).
    min_confidence_to_act: Mapped[Decimal] = mapped_column(
        Numeric(3, 2), nullable=False, default=Decimal("0.60"),
        server_default="0.60",
    )

    # Quiet hours (UTC, 0-23).  When ``now`` falls inside the window the
    # notification row still persists but the alert EMAIL is deferred
    # (the next sweep / digest picks it up).  NULL = no quiet hours.
    # A start > end window wraps midnight (e.g. 22 → 6).
    quiet_hours_start_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quiet_hours_end_utc: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Notification(TenantMixin, Base):
    """A persisted alert for the operator (powers the UI bell/feed).

    ``dedup_key`` is the idempotency anchor — e.g.
    ``positive_reply:<message_id>`` or ``task_due:<activity_id>``.
    Insertion goes through ``services.notifications.create_notification``
    which treats a unique-violation as "already notified, skip".
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Per-tenant dedup (Phase 2); NULLS NOT DISTINCT keeps legacy
        # worker rows (tenant_id NULL) deduping app-wide as before.
        UniqueConstraint("tenant_id", "dedup_key",
                         name="uq_notifications_tenant_dedup",
                         postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, name="notification_kind",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
        nullable=True,
    )
    activity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_activities.id", ondelete="SET NULL"),
        nullable=True,
    )

    dedup_key: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
    )

    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Stamped when the owner-alert email actually went out.  NULL +
    # old created_at = deferred by quiet hours (or OWNER_NOTIFY_EMAIL
    # unset); the digest sweeps those up.
    emailed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class AgentAction(TenantMixin, Base):
    """Append-only audit log of every autonomous agent decision.

    One row per decision INCLUDING skips ("confidence below threshold")
    and failures ("classifier returned unparseable JSON") so the
    operator can always answer "why did/didn't the agent do X?".
    """

    __tablename__ = "agent_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    action_type: Mapped[AgentActionType] = mapped_column(
        Enum(AgentActionType, name="agent_action_type",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    status: Mapped[AgentActionStatus] = mapped_column(
        Enum(AgentActionStatus, name="agent_action_status",
             values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_opportunities.id", ondelete="SET NULL"),
        nullable=True,
    )
    activity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("crm_activities.id", ondelete="SET NULL"),
        nullable=True,
    )

    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Model output, confidence, reason-for-skip, cost breakdown, etc.
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        index=True,
    )
