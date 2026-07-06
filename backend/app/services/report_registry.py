"""Server-side whitelist registry for the custom report builder.

The report builder is **metadata-driven and never executes user SQL**.  Every
reportable object, field, operator, and aggregate the user can pick resolves
through THIS registry; anything not declared here is rejected before a query
is built (see ``report_query.py``).

Adding a reportable field is a deliberate, reviewed act of declaring it here —
that allowlist is the security boundary.  Tenancy-ready: each object notes
whether its model carries a ``tenant_id`` so the query layer can scope later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import (
    Account,
    Contact,
    CrmActivity,
    CrmActivityDirection,
    CrmActivityType,
    CrmLeadStatus,
    Lead,
    Opportunity,
    OpportunityStage,
    SendStatus,
)

# --------------------------------------------------------------------------
# Field types -> allowed operators / aggregates
# --------------------------------------------------------------------------

# Operators legal per field type.  The query layer maps each to a
# parameterized SQLAlchemy expression — no string interpolation of values.
OPERATORS_BY_TYPE: dict[str, list[str]] = {
    "string": [
        "equals", "not_equals", "contains", "not_contains",
        "starts_with", "in", "is_empty", "is_not_empty",
    ],
    "enum": ["equals", "not_equals", "in", "is_empty", "is_not_empty"],
    "number": [
        "equals", "not_equals", "gt", "gte", "lt", "lte",
        "between", "is_empty", "is_not_empty",
    ],
    "date": [
        "equals", "before", "after", "between",
        "relative_range", "is_empty", "is_not_empty",
    ],
    "datetime": [
        "before", "after", "between", "relative_range",
        "is_empty", "is_not_empty",
    ],
    "boolean": ["equals"],
}

# Aggregate functions legal per field type ('count' is always available at the
# row level, handled separately).
AGGREGATES_BY_TYPE: dict[str, list[str]] = {
    "number": ["sum", "avg", "min", "max", "count"],
    "string": ["count"],
    "enum": ["count"],
    "date": ["min", "max", "count"],
    "datetime": ["min", "max", "count"],
    "boolean": ["count"],
}

# Relative date-range keywords the date/datetime ``relative_range`` op accepts.
RELATIVE_RANGES = [
    "today", "yesterday", "last_7_days", "last_30_days", "last_90_days",
    "this_month", "this_year", "year_to_date",
]


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    type: str               # string | enum | number | date | datetime | boolean
    column: Any             # SQLAlchemy column expression (InstrumentedAttribute)
    join: str | None = None  # join key (into ReportableObject.joins) if related
    enum_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class JoinDef:
    target: Any             # SQLAlchemy model to outerjoin
    onclause: Any           # join condition expression


@dataclass(frozen=True)
class ReportableObject:
    key: str
    label: str
    model: Any
    fields: dict[str, FieldDef]
    joins: dict[str, JoinDef] = field(default_factory=dict)
    default_columns: tuple[str, ...] = ()
    has_tenant: bool = True   # model carries tenant_id (query layer scopes later)


def _enum_values(enum_cls) -> tuple[str, ...]:
    return tuple(m.value for m in enum_cls)


# --------------------------------------------------------------------------
# Object definitions (the allowlist)
# --------------------------------------------------------------------------

_LEADS = ReportableObject(
    key="leads", label="Leads", model=Lead,
    fields={
        "email": FieldDef("email", "Email", "string", Lead.email),
        "first_name": FieldDef("first_name", "First name", "string", Lead.first_name),
        "last_name": FieldDef("last_name", "Last name", "string", Lead.last_name),
        "company": FieldDef("company", "Company", "string", Lead.company),
        "job_title": FieldDef("job_title", "Job title", "string", Lead.job_title),
        "crm_status": FieldDef(
            "crm_status", "CRM status", "enum", Lead.crm_status,
            enum_values=_enum_values(CrmLeadStatus),
        ),
        "send_status": FieldDef(
            "send_status", "Send status", "enum", Lead.send_status,
            enum_values=_enum_values(SendStatus),
        ),
        "created_at": FieldDef("created_at", "Created", "datetime", Lead.created_at),
    },
    default_columns=("first_name", "last_name", "email", "company", "crm_status", "created_at"),
)

_OPPORTUNITIES = ReportableObject(
    key="opportunities", label="Opportunities", model=Opportunity,
    fields={
        "name": FieldDef("name", "Name", "string", Opportunity.name),
        "stage": FieldDef(
            "stage", "Stage", "enum", Opportunity.stage,
            enum_values=_enum_values(OpportunityStage),
        ),
        "amount": FieldDef("amount", "Amount", "number", Opportunity.amount),
        "probability": FieldDef("probability", "Probability", "number", Opportunity.probability),
        "close_date": FieldDef("close_date", "Close date", "date", Opportunity.close_date),
        "company": FieldDef("company", "Company", "string", Opportunity.company),
        "email": FieldDef("email", "Contact email", "string", Opportunity.email),
        "loss_reason": FieldDef("loss_reason", "Loss reason", "string", Opportunity.loss_reason),
        "created_at": FieldDef("created_at", "Created", "datetime", Opportunity.created_at),
        "closed_at": FieldDef("closed_at", "Closed at", "datetime", Opportunity.closed_at),
        "account_name": FieldDef(
            "account_name", "Account", "string", Account.name, join="account",
        ),
    },
    joins={
        "account": JoinDef(Account, Opportunity.account_id == Account.id),
    },
    default_columns=("name", "stage", "amount", "close_date", "company"),
)

_ACTIVITIES = ReportableObject(
    key="activities", label="Activities", model=CrmActivity,
    fields={
        "activity_type": FieldDef(
            "activity_type", "Type", "enum", CrmActivity.activity_type,
            enum_values=_enum_values(CrmActivityType),
        ),
        "subject": FieldDef("subject", "Subject", "string", CrmActivity.subject),
        "direction": FieldDef(
            "direction", "Direction", "enum", CrmActivity.direction,
            enum_values=_enum_values(CrmActivityDirection),
        ),
        "sentiment": FieldDef("sentiment", "Sentiment", "string", CrmActivity.sentiment),
        "is_agent_generated": FieldDef(
            "is_agent_generated", "Agent-generated", "boolean", CrmActivity.is_agent_generated,
        ),
        "due_at": FieldDef("due_at", "Due", "datetime", CrmActivity.due_at),
        "completed_at": FieldDef("completed_at", "Completed", "datetime", CrmActivity.completed_at),
        "occurred_at": FieldDef("occurred_at", "Occurred", "datetime", CrmActivity.occurred_at),
        "created_at": FieldDef("created_at", "Created", "datetime", CrmActivity.created_at),
    },
    default_columns=("activity_type", "subject", "direction", "occurred_at"),
)

_CONTACTS = ReportableObject(
    key="contacts", label="Contacts", model=Contact,
    fields={
        "first_name": FieldDef("first_name", "First name", "string", Contact.first_name),
        "last_name": FieldDef("last_name", "Last name", "string", Contact.last_name),
        "email": FieldDef("email", "Email", "string", Contact.email),
        "job_title": FieldDef("job_title", "Job title", "string", Contact.job_title),
        "created_at": FieldDef("created_at", "Created", "datetime", Contact.created_at),
        "account_name": FieldDef(
            "account_name", "Account", "string", Account.name, join="account",
        ),
    },
    joins={
        "account": JoinDef(Account, Contact.account_id == Account.id),
    },
    default_columns=("first_name", "last_name", "email", "job_title", "created_at"),
)

_ACCOUNTS = ReportableObject(
    key="accounts", label="Accounts", model=Account,
    fields={
        "name": FieldDef("name", "Name", "string", Account.name),
        "domain": FieldDef("domain", "Domain", "string", Account.domain),
        "industry": FieldDef("industry", "Industry", "string", Account.industry),
        "size_hint": FieldDef("size_hint", "Size", "string", Account.size_hint),
        "created_at": FieldDef("created_at", "Created", "datetime", Account.created_at),
    },
    default_columns=("name", "domain", "industry", "created_at"),
)

REGISTRY: dict[str, ReportableObject] = {
    o.key: o for o in (_LEADS, _OPPORTUNITIES, _ACTIVITIES, _CONTACTS, _ACCOUNTS)
}


def get_object(key: str) -> ReportableObject | None:
    return REGISTRY.get(key)


def aggregates_for(ftype: str) -> list[str]:
    return AGGREGATES_BY_TYPE.get(ftype, ["count"])


def build_metadata() -> dict[str, Any]:
    """The full whitelist as JSON for the builder UI: objects -> fields with
    their type, allowed operators, and allowed aggregates."""
    objects = []
    for obj in REGISTRY.values():
        fields_meta = []
        for f in obj.fields.values():
            fields_meta.append({
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "operators": OPERATORS_BY_TYPE.get(f.type, []),
                "aggregates": aggregates_for(f.type),
                "groupable": True,
                "enum_values": list(f.enum_values) if f.enum_values else None,
            })
        # stable, label-sorted for the UI
        fields_meta.sort(key=lambda m: m["label"])
        objects.append({
            "key": obj.key,
            "label": obj.label,
            "fields": fields_meta,
            "default_columns": list(obj.default_columns),
        })
    objects.sort(key=lambda o: o["label"])
    return {
        "objects": objects,
        "relative_ranges": RELATIVE_RANGES,
    }
