"""Phase 1 — CRM data-model foundation (migration 0038).

Schema-level tests: the new tables (pipelines, opportunity_stages, accounts,
contacts, opportunity_stage_changes, report_definitions) persist with their
relationships + nullable tenant_id, and the additive FK/owner/tenant columns
on opportunities / activities / leads work.  No UI / endpoints in this phase.

NOTE: the test DB is built via ``Base.metadata.create_all`` (conftest), NOT
the Alembic migration — so the seeded default pipeline does NOT exist here;
each test creates its own pipeline/stages.  The migration's seed + backfill
are verified separately against the dev DB.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import (
    Account,
    Contact,
    CrmActivity,
    CrmActivityType,
    Lead,
    Opportunity,
    OpportunityStage,
    OpportunityStageChange,
    Pipeline,
    PipelineStage,
    ReportDefinition,
)

# Since Phase 2, tenant_id carries an FK — rows must reference the real
# per-test bootstrap tenant, not an arbitrary UUID.
from tests.conftest import BOOTSTRAP_TENANT_ID as TENANT  # noqa: E402
OWNER = uuid.uuid4()


async def _default_pipeline(db_session) -> tuple[Pipeline, list[PipelineStage]]:
    """Create a default pipeline + 6 stages mirroring the legacy enum."""
    p = Pipeline(name="Default", is_default=True, tenant_id=TENANT)
    db_session.add(p)
    await db_session.flush()
    seed = [
        ("prospecting", "Prospecting", 0, 10, False, False),
        ("qualification", "Qualification", 1, 25, False, False),
        ("proposal", "Proposal", 2, 50, False, False),
        ("negotiation", "Negotiation", 3, 75, False, False),
        ("closed_won", "Closed Won", 4, 100, True, False),
        ("closed_lost", "Closed Lost", 5, 0, False, True),
    ]
    stages = [
        PipelineStage(
            pipeline_id=p.id, tenant_id=TENANT, key=k, name=n, sort_order=o,
            default_probability=prob, is_won=won, is_lost=lost,
        )
        for k, n, o, prob, won, lost in seed
    ]
    db_session.add_all(stages)
    await db_session.commit()
    return p, stages


async def test_pipeline_and_stages_persist_ordered(db_session):
    p, _ = await _default_pipeline(db_session)
    # Async: eager-load the relationship explicitly (codebase convention —
    # relationships aren't lazy-accessed in async sessions).
    reloaded = (await db_session.execute(
        select(Pipeline).options(selectinload(Pipeline.stages)).where(Pipeline.id == p.id)
    )).scalar_one()
    # relationship loads ordered by sort_order
    keys = [s.key for s in reloaded.stages]
    assert keys == [
        "prospecting", "qualification", "proposal",
        "negotiation", "closed_won", "closed_lost",
    ]
    won = [s for s in reloaded.stages if s.is_won]
    lost = [s for s in reloaded.stages if s.is_lost]
    assert [s.key for s in won] == ["closed_won"]
    assert [s.key for s in lost] == ["closed_lost"]
    assert reloaded.is_default is True
    assert reloaded.tenant_id == TENANT


async def test_account_contact_graph(db_session):
    acct = Account(name="Acme Inc", domain="acme.com", tenant_id=TENANT, owner_id=OWNER)
    db_session.add(acct)
    await db_session.flush()
    c1 = Contact(account_id=acct.id, first_name="Ada", email="ada@acme.com", tenant_id=TENANT)
    c2 = Contact(account_id=acct.id, first_name="Bob", email="bob@acme.com", tenant_id=TENANT)
    db_session.add_all([c1, c2])
    await db_session.commit()

    reloaded = (await db_session.execute(
        select(Account)
        .options(selectinload(Account.contacts).selectinload(Contact.account))
        .where(Account.id == acct.id)
    )).scalar_one()
    assert {c.first_name for c in reloaded.contacts} == {"Ada", "Bob"}
    # forward + back relationship both resolve (eager-loaded)
    assert reloaded.contacts[0].account.name == "Acme Inc"
    assert all(c.account_id == acct.id for c in reloaded.contacts)
    assert reloaded.owner_id == OWNER


async def test_opportunity_links_pipeline_stage_account_contact(db_session):
    p, stages = await _default_pipeline(db_session)
    proposal = next(s for s in stages if s.key == "proposal")
    acct = Account(name="Acme", tenant_id=TENANT)
    db_session.add(acct)
    await db_session.flush()
    contact = Contact(account_id=acct.id, first_name="Ada", tenant_id=TENANT)
    db_session.add(contact)
    await db_session.flush()

    opp = Opportunity(
        name="Acme deal",
        stage=OpportunityStage.PROPOSAL,   # legacy enum still dual-written
        stage_id=proposal.id,
        pipeline_id=p.id,
        account_id=acct.id,
        contact_id=contact.id,
        owner_id=OWNER,
        tenant_id=TENANT,
        amount=5000,
        close_date=date(2026, 12, 31),
    )
    db_session.add(opp)
    await db_session.commit()

    reloaded = await db_session.get(Opportunity, opp.id)
    assert reloaded.stage == OpportunityStage.PROPOSAL
    assert reloaded.stage_id == proposal.id
    assert reloaded.pipeline_id == p.id
    assert reloaded.account_id == acct.id
    assert reloaded.contact_id == contact.id
    assert reloaded.owner_id == OWNER
    assert reloaded.tenant_id == TENANT


async def test_opportunity_stage_change_audit_row(db_session):
    p, stages = await _default_pipeline(db_session)
    prospecting = next(s for s in stages if s.key == "prospecting")
    proposal = next(s for s in stages if s.key == "proposal")
    opp = Opportunity(name="deal", stage=OpportunityStage.PROPOSAL, tenant_id=TENANT)
    db_session.add(opp)
    await db_session.flush()

    change = OpportunityStageChange(
        opportunity_id=opp.id,
        from_stage_id=prospecting.id, to_stage_id=proposal.id,
        from_stage_key="prospecting", to_stage_key="proposal",
        changed_by=OWNER, source="user", tenant_id=TENANT,
    )
    db_session.add(change)
    await db_session.commit()

    rows = (await db_session.execute(
        select(OpportunityStageChange).where(
            OpportunityStageChange.opportunity_id == opp.id
        )
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].from_stage_key == "prospecting"
    assert rows[0].to_stage_key == "proposal"
    assert rows[0].source == "user"
    assert rows[0].changed_by == OWNER


async def test_report_definition_round_trips_json(db_session):
    definition = {
        "object": "opportunities",
        "columns": ["name", "stage", "amount"],
        "filters": [{"field": "amount", "op": "gte", "value": 5000}],
        "group_by": ["stage"],
        "aggregates": [{"field": "amount", "fn": "sum"}],
        "sort": [{"field": "amount", "dir": "desc"}],
    }
    rd = ReportDefinition(
        name="Big open deals", data_source="opportunities",
        definition=definition, owner_id=OWNER, tenant_id=TENANT,
    )
    db_session.add(rd)
    await db_session.commit()

    reloaded = await db_session.get(ReportDefinition, rd.id)
    assert reloaded.data_source == "opportunities"
    assert reloaded.definition["filters"][0]["op"] == "gte"
    assert reloaded.definition["group_by"] == ["stage"]
    assert reloaded.tenant_id == TENANT


async def test_crm_activity_new_fields(db_session):
    acct = Account(name="Acme", tenant_id=TENANT)
    db_session.add(acct)
    await db_session.flush()
    contact = Contact(account_id=acct.id, first_name="Ada", tenant_id=TENANT)
    opp = Opportunity(name="deal", stage=OpportunityStage.PROSPECTING, tenant_id=TENANT)
    db_session.add_all([contact, opp])
    await db_session.flush()

    act = CrmActivity(
        opportunity_id=opp.id,          # satisfies the has-parent CHECK
        account_id=acct.id,
        contact_id=contact.id,
        owner_id=OWNER,
        tenant_id=TENANT,
        activity_type=CrmActivityType.CALL,
        subject="Intro call",
    )
    db_session.add(act)
    await db_session.commit()

    reloaded = await db_session.get(CrmActivity, act.id)
    assert reloaded.account_id == acct.id
    assert reloaded.contact_id == contact.id
    assert reloaded.owner_id == OWNER
    assert reloaded.tenant_id == TENANT


async def test_lead_tenant_id_nullable_and_settable(db_session):
    lead = Lead(email="x@y.com")
    db_session.add(lead)
    await db_session.commit()
    assert lead.tenant_id is None  # nullable, defaults to NULL

    lead.tenant_id = TENANT
    await db_session.commit()
    reloaded = await db_session.get(Lead, lead.id)
    assert reloaded.tenant_id == TENANT
