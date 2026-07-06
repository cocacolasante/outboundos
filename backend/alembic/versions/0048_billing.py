"""Multi-tenancy Phase 5 (Stripe billing).

- Billing columns on ``tenants`` (one subscription per tenant — Stripe is
  the system of record; these are the webhook-projected current state).
- ``usage_counters``: per-(tenant, month, meter) atomic counters backing
  the quota checks — born with NOT NULL tenant_id + FK + RLS inline.

Existing tenants keep NULL ``subscription_status`` = legacy/unbilled
(treated as an open-ended trial by the entitlements service until the
hardening phase).
"""
from typing import Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels = None
depends_on = None

_PREDICATE = "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.execute("CREATE TYPE billing_plan AS ENUM ('starter', 'pro', 'agency')")
    op.execute(
        "CREATE TYPE subscription_status AS ENUM "
        "('trialing', 'active', 'past_due', 'canceled', 'incomplete')"
    )

    op.add_column("tenants", sa.Column("stripe_customer_id", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_tenants_stripe_customer", "tenants",
                                ["stripe_customer_id"])
    op.add_column("tenants", sa.Column("stripe_subscription_id", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column(
        "plan",
        postgresql.ENUM("starter", "pro", "agency",
                        name="billing_plan", create_type=False),
        nullable=True,
    ))
    op.add_column("tenants", sa.Column(
        "subscription_status",
        postgresql.ENUM("trialing", "active", "past_due", "canceled", "incomplete",
                        name="subscription_status", create_type=False),
        nullable=True,
    ))
    op.add_column("tenants", sa.Column("current_period_end",
                                       sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("trial_ends_at",
                                       sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "usage_counters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("meter", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "period", "meter",
                            name="uq_usage_counters_tenant_period_meter",
                            postgresql_nulls_not_distinct=True),
    )
    op.execute("ALTER TABLE usage_counters ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON usage_counters "
        f"USING {_PREDICATE} WITH CHECK {_PREDICATE}"
    )


def downgrade() -> None:
    op.drop_table("usage_counters")
    op.drop_column("tenants", "trial_ends_at")
    op.drop_column("tenants", "current_period_end")
    op.drop_column("tenants", "subscription_status")
    op.drop_column("tenants", "plan")
    op.drop_column("tenants", "stripe_subscription_id")
    op.drop_constraint("uq_tenants_stripe_customer", "tenants", type_="unique")
    op.drop_column("tenants", "stripe_customer_id")
    op.execute("DROP TYPE IF EXISTS subscription_status")
    op.execute("DROP TYPE IF EXISTS billing_plan")
