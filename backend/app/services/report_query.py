"""Metadata-driven report query service.

Maps a saved/ad-hoc report *definition* (structured JSON) to a SAFE,
parameterized SQLAlchemy query over the whitelist in ``report_registry``.

Hard rules:
- **No raw user SQL, ever.**  Every column/filter/group/sort/aggregate is
  resolved through the registry; an unknown reference raises ``ReportError``
  (the router returns 400) before any query is built.
- All user *values* bind as SQL parameters (SQLAlchemy expression API) — never
  string-interpolated into SQL.
- Queries accept an optional ``tenant_id`` filter (applied only when the model
  carries ``tenant_id`` AND a tenant is passed) so the future tenant refactor
  drops in cleanly.
"""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.report_registry import (
    OPERATORS_BY_TYPE,
    REGISTRY,
    FieldDef,
    aggregates_for,
)

ROW_CAP = 5000
_AGG_FUNCS = {"sum": func.sum, "avg": func.avg, "min": func.min, "max": func.max, "count": func.count}


class ReportError(ValueError):
    """A definition that fails validation against the whitelist."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Value coercion (all raise ReportError on bad input — never crash a request)
# --------------------------------------------------------------------------

def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ReportError(f"expected a number, got {v!r}")


def _pair(v: Any) -> tuple[Any, Any]:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ReportError("'between' needs a [low, high] pair")
    return v[0], v[1]


def _dt(v: Any, ftype: str):
    if not isinstance(v, str):
        raise ReportError(f"expected an ISO date string, got {v!r}")
    try:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(v), time.min)
        except ValueError:
            raise ReportError(f"invalid date '{v}'")
    if ftype == "date":
        return parsed.date()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_relative_range(keyword: Any) -> tuple[datetime, datetime]:
    now = _now()
    today = now.date()
    start_of_today = datetime.combine(today, time.min, tzinfo=timezone.utc)
    if keyword == "today":
        return start_of_today, now
    if keyword == "yesterday":
        return start_of_today - timedelta(days=1), start_of_today
    if keyword == "last_7_days":
        return now - timedelta(days=7), now
    if keyword == "last_30_days":
        return now - timedelta(days=30), now
    if keyword == "last_90_days":
        return now - timedelta(days=90), now
    if keyword == "this_month":
        return datetime(today.year, today.month, 1, tzinfo=timezone.utc), now
    if keyword in ("this_year", "year_to_date"):
        return datetime(today.year, 1, 1, tzinfo=timezone.utc), now
    raise ReportError(f"unknown relative range '{keyword}'")


def _serialize(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, enum.Enum):
        return v.value
    return v


# --------------------------------------------------------------------------
# Filter -> SQLAlchemy expression (values bind as parameters)
# --------------------------------------------------------------------------

def _filter_expr(f: FieldDef, op: str, value: Any):
    allowed = OPERATORS_BY_TYPE.get(f.type, [])
    if op not in allowed:
        raise ReportError(f"operator '{op}' not allowed on '{f.key}' ({f.type})")
    c = f.column
    t = f.type

    if op == "is_empty":
        return or_(c.is_(None), c == "") if t in ("string", "enum") else c.is_(None)
    if op == "is_not_empty":
        return and_(c.isnot(None), c != "") if t in ("string", "enum") else c.isnot(None)

    if t in ("string", "enum"):
        if op == "equals":
            return c == value
        if op == "not_equals":
            return c != value
        if op == "contains":
            return c.ilike(f"%{value}%")
        if op == "not_contains":
            return ~c.ilike(f"%{value}%")
        if op == "starts_with":
            return c.ilike(f"{value}%")
        if op == "in":
            if not isinstance(value, list):
                raise ReportError("'in' needs a list of values")
            return c.in_(value)

    if t == "number":
        if op == "between":
            lo, hi = _pair(value)
            return c.between(_num(lo), _num(hi))
        n = _num(value)
        return {
            "equals": c == n, "not_equals": c != n,
            "gt": c > n, "gte": c >= n, "lt": c < n, "lte": c <= n,
        }[op]

    if t in ("date", "datetime"):
        if op == "relative_range":
            start, end = _resolve_relative_range(value)
            return c.between(start, end)
        if op == "between":
            lo, hi = _pair(value)
            return c.between(_dt(lo, t), _dt(hi, t))
        d = _dt(value, t)
        return {"equals": c == d, "before": c < d, "after": c > d}[op]

    if t == "boolean":
        truthy = value in (True, "true", "True", 1, "1")
        return c.is_(truthy)

    raise ReportError(f"unsupported operator '{op}' for type '{t}'")


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def _build_query(
    data_source: str,
    definition: dict[str, Any] | None,
    *,
    tenant_id=None,
    row_cap: int = ROW_CAP,
):
    """Resolve a definition against the whitelist into a (stmt, columns, limit)
    triple.  Pure (no DB) so it doubles as the save-time validator — any bad
    reference raises ``ReportError`` here, before execution."""
    obj = REGISTRY.get(data_source)
    if obj is None:
        raise ReportError(f"unknown data source '{data_source}'")
    definition = definition or {}

    columns = definition.get("columns") or []
    filters = definition.get("filters") or []
    group_by = definition.get("group_by") or []
    aggregates = definition.get("aggregates") or []
    sort = definition.get("sort") or []
    try:
        limit = int(definition.get("limit") or row_cap)
    except (TypeError, ValueError):
        raise ReportError("limit must be an integer")
    limit = max(1, min(limit, row_cap))

    grouped = bool(group_by) or bool(aggregates)
    needed_joins: set[str] = set()
    result_columns: list[dict[str, Any]] = []
    select_exprs: list[Any] = []
    label_to_expr: dict[str, Any] = {}

    def fdef(key: str) -> FieldDef:
        if not key or key not in obj.fields:
            raise ReportError(f"unknown field '{key}' for '{data_source}'")
        f = obj.fields[key]
        if f.join:
            needed_joins.add(f.join)
        return f

    def add_expr(label: str, expr: Any, col_meta: dict[str, Any]) -> None:
        labeled = expr.label(label)
        select_exprs.append(labeled)
        label_to_expr[label] = labeled
        result_columns.append(col_meta)

    if grouped:
        for gkey in group_by:
            f = fdef(gkey)
            add_expr(gkey, f.column, {"key": gkey, "label": f.label, "type": f.type})
        if not aggregates:
            aggregates = [{"fn": "count"}]
        for agg in aggregates:
            fn = (agg or {}).get("fn")
            akey = (agg or {}).get("field")
            if fn == "count" and not akey:
                add_expr("count", func.count(), {"key": "count", "label": "Count", "type": "number"})
                continue
            if fn not in _AGG_FUNCS:
                raise ReportError(f"unknown aggregate '{fn}'")
            f = fdef(akey)
            if fn not in aggregates_for(f.type):
                raise ReportError(f"aggregate '{fn}' not allowed on '{akey}' ({f.type})")
            alias = f"{akey}_{fn}"
            rtype = "number" if fn in ("sum", "avg", "count") else f.type
            add_expr(alias, _AGG_FUNCS[fn](f.column),
                     {"key": alias, "label": f"{f.label} ({fn})", "type": rtype})
    else:
        cols = columns or list(obj.default_columns)
        if not cols:
            raise ReportError("no columns selected")
        for ckey in cols:
            f = fdef(ckey)
            add_expr(ckey, f.column, {"key": ckey, "label": f.label, "type": f.type})

    # Resolve filter fields (collects joins) before building the statement.
    filter_specs: list[Any] = []
    for filt in filters:
        if not isinstance(filt, dict):
            raise ReportError("each filter must be an object")
        f = fdef(filt.get("field"))
        filter_specs.append(_filter_expr(f, filt.get("op"), filt.get("value")))

    # Sort fields must reference a selected column / group field / aggregate.
    order_specs: list[Any] = []
    for s in sort:
        skey = (s or {}).get("field")
        if skey not in label_to_expr:
            raise ReportError(f"cannot sort by '{skey}' — not a selected column")
        direction = (s or {}).get("dir", "asc")
        expr = label_to_expr[skey]
        order_specs.append(expr.desc() if direction == "desc" else expr.asc())

    # ---- assemble ----
    stmt = select(*select_exprs).select_from(obj.model)
    for jkey in needed_joins:
        jd = obj.joins[jkey]
        stmt = stmt.outerjoin(jd.target, jd.onclause)

    conditions = list(filter_specs)
    # Tenancy-ready: scope when a tenant is supplied and the model carries it.
    if tenant_id is not None and obj.has_tenant and hasattr(obj.model, "tenant_id"):
        conditions.append(obj.model.tenant_id == tenant_id)
    if conditions:
        stmt = stmt.where(and_(*conditions))

    if grouped:
        stmt = stmt.group_by(*[obj.fields[g].column for g in group_by])
    if order_specs:
        stmt = stmt.order_by(*order_specs)
    stmt = stmt.limit(limit)

    return stmt, result_columns, grouped, limit


def validate_definition(data_source: str, definition: dict[str, Any] | None) -> None:
    """Raise ``ReportError`` if the definition references anything outside the
    whitelist.  Used at save time so stored reports are always runnable."""
    _build_query(data_source, definition)


async def run_report(
    db: AsyncSession,
    data_source: str,
    definition: dict[str, Any] | None,
    *,
    tenant_id=None,
    row_cap: int = ROW_CAP,
) -> dict[str, Any]:
    stmt, result_columns, grouped, limit = _build_query(
        data_source, definition, tenant_id=tenant_id, row_cap=row_cap,
    )
    result = await db.execute(stmt)
    raw_rows = result.all()
    keys = [c["key"] for c in result_columns]
    rows = [
        {k: _serialize(row._mapping[k]) for k in keys}
        for row in raw_rows
    ]
    return {
        "data_source": data_source,
        "columns": result_columns,
        "rows": rows,
        "row_count": len(rows),
        "grouped": grouped,
        "truncated": len(rows) >= limit,
        "limit": limit,
    }
