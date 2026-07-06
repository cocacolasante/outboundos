"""Phase 7: compose_lead worker (Anthropic mocked, real DB)."""
import uuid
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    ComposeStatus,
    Lead,
    ResearchMode,
    Sequence,
    SequenceNode,
    SequenceNodeKind,
    StyleCorrection,
)
from app.workers import compose as compose_mod


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _anthropic_text(body: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=body)])


async def _make_campaign(
    db_session, *,
    status: CampaignStatus = CampaignStatus.PREVIEWING,
) -> Campaign:
    c = Campaign(
        name="Phase 7 test",
        goal="Book a discovery call",
        tone="Direct",
        sender_name="Anthony",
        sender_email="a@x.com",
        sample_count=1,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0),
        schedule_time_end=time(17, 0),
        status=status,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


async def _make_lead(db_session, campaign: Campaign, **overrides) -> Lead:
    defaults = {
        "email": "lead@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Acme",
        "job_title": "CEO",
        "research_data": {"quality": "rich", "person_news": ["raised Series B"]},
    }
    defaults.update(overrides)
    l = Lead(campaign_id=campaign.id, **defaults)
    db_session.add(l)
    await db_session.commit()
    await db_session.refresh(l)
    return l


def _patch_create(create_mock: AsyncMock):
    """Patch _get_client to return a stub with messages.create=create_mock."""
    return patch.object(
        compose_mod, "_get_client",
        return_value=SimpleNamespace(
            messages=SimpleNamespace(create=create_mock)
        ),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    compose_mod._client = None
    yield
    compose_mod._client = None


# --------------------------------------------------------------------------
# Prompt selection
# --------------------------------------------------------------------------


async def test_skips_compose_when_entry_node_is_not_email(db_session):
    """When the sequence starts with a non-email node there's no first email
    to write — compose skips the AI call + send and leaves the body empty."""
    campaign = await _make_campaign(db_session)
    seq = Sequence(campaign_id=campaign.id, is_published=True)
    db_session.add(seq)
    await db_session.flush()
    db_session.add(SequenceNode(
        sequence_id=seq.id,
        kind=SequenceNodeKind.LINKEDIN_CONNECT,
        config={},
        position_x=0,
        position_y=0,
        is_entry=True,
    ))
    await db_session.commit()

    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock()  # must NOT be called
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay") as send_delay:
        result = await compose_mod.compose_lead_async(str(lead.id))

    create_mock.assert_not_called()
    send_delay.assert_not_called()
    assert result["status"] == "skipped_non_email_entry"

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.compose_status == ComposeStatus.DONE
    assert refreshed.composed_body is None


async def test_template_mode_renders_without_calling_anthropic(db_session):
    campaign = await _make_campaign(db_session)
    campaign.research_mode = ResearchMode.TEMPLATE
    campaign.template_subject = "Quick question about {{company}}"
    campaign.template_body = "Hi {{first_name|there}},\n\nLove what {{company}} does in {{Industry}}."
    await db_session.commit()

    lead = await _make_lead(
        db_session, campaign,
        first_name="",  # exercise the inline default
        company="Acme",
        raw_csv_row={"Industry": "fintech"},
    )

    create_mock = AsyncMock()  # must NOT be called
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    create_mock.assert_not_called()
    assert result["status"] == "done"

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.compose_status == ComposeStatus.DONE
    assert refreshed.composed_subject == "Quick question about Acme"
    assert refreshed.composed_body == "Hi there,\n\nLove what Acme does in fintech."


async def test_campaign_signature_replaces_ai_signoff(db_session):
    campaign = await _make_campaign(db_session)
    campaign.signature = "Anthony Colasante\n555-1234\nacme.com\ncal.com/anthony"
    await db_session.commit()
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "Quick question", "body": "Hi Jane, value here.\\n\\nBest,\\nAnthony"}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "done"
    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.composed_body.endswith(
        "Anthony Colasante\n555-1234\nacme.com\ncal.com/anthony"
    )
    assert "Best,\nAnthony" not in refreshed.composed_body  # AI sign-off swapped


async def test_low_quality_lead_uses_generic_prompt(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(
        db_session, campaign,
        research_data={"quality": "low", "person_news": [], "company_description": ""},
    )

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "Quick question", "body": "Hi Jane, value prop here."}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "done"
    system_prompt = create_mock.call_args.kwargs["system"]
    assert "Research on this person was limited" in system_prompt
    # Personalized-only hints must NOT appear.
    assert "LinkedIn headline" not in system_prompt
    assert "Recent person news" not in system_prompt


async def test_rich_quality_lead_uses_personalized_prompt(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign, research_data={
        "quality": "rich",
        "person_news": ["raised Series B"],
        "company_news": ["launched X"],
        "company_description": "AI for SMB",
        "recent_updates": ["new partnership"],
        "linkedin_headline": "CEO at Acme",
    })

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "Series B + AI", "body": "Hi Jane, saw your Series B..."}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    system_prompt = create_mock.call_args.kwargs["system"]
    assert "raised Series B" in system_prompt
    assert "CEO at Acme" in system_prompt
    assert "AI for SMB" in system_prompt
    assert "Research on this person was limited" not in system_prompt


async def test_partial_quality_also_uses_personalized_prompt(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign, research_data={
        "quality": "partial",
        "person_news": [],
        "company_description": "AI startup",
    })

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "S", "body": "B"}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    system_prompt = create_mock.call_args.kwargs["system"]
    assert "AI startup" in system_prompt
    assert "Research on this person was limited" not in system_prompt


# --------------------------------------------------------------------------
# Style corrections
# --------------------------------------------------------------------------


async def test_style_corrections_included_in_personalized_prompt(db_session):
    campaign = await _make_campaign(db_session)
    db_session.add(StyleCorrection(
        campaign_id=campaign.id,
        original_body="Original verbose",
        corrected_body="Tight punchy version",
    ))
    db_session.add(StyleCorrection(
        campaign_id=campaign.id,
        original_body="Another",
        corrected_body="Another short rewrite",
    ))
    await db_session.commit()

    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    system_prompt = create_mock.call_args.kwargs["system"]
    assert "Tight punchy version" in system_prompt
    assert "Another short rewrite" in system_prompt
    assert "MATCH THIS STYLE CLOSELY" in system_prompt


async def test_personalized_prompt_when_no_corrections(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    system_prompt = create_mock.call_args.kwargs["system"]
    assert "No style corrections yet" in system_prompt


# --------------------------------------------------------------------------
# JSON parsing + retry
# --------------------------------------------------------------------------


async def test_retries_with_stricter_prompt_on_parse_failure(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(side_effect=[
        _anthropic_text("not json at all"),
        _anthropic_text('{"subject": "Hi", "body": "Body text"}'),
    ])
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "done"
    assert create_mock.call_count == 2
    # The stricter retry should include the explicit instruction.
    second_call_system = create_mock.call_args_list[1].kwargs["system"]
    assert "CRITICAL" in second_call_system


async def test_marks_failed_when_both_attempts_fail_to_parse(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(side_effect=[
        _anthropic_text("garbage 1"),
        _anthropic_text("garbage 2"),
    ])
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay") as enqueue:
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "parse_failed"
    enqueue.assert_not_called()

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.compose_status == ComposeStatus.FAILED


async def test_strips_markdown_fences_before_parsing(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text(
        '```json\n{"subject": "Hello", "body": "Hi"}\n```'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "done"
    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.composed_subject == "Hello"


async def test_rejects_empty_subject_or_body(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(side_effect=[
        _anthropic_text('{"subject": "", "body": "Hi"}'),
        _anthropic_text('{"subject": "OK", "body": ""}'),
    ])
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "parse_failed"


# --------------------------------------------------------------------------
# Persistence + style sanitisation
# --------------------------------------------------------------------------


async def test_does_not_append_unsubscribe_footer(db_session):
    """These are individual person-to-person messages; no CAN-SPAM footer."""
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "Hi", "body": "Hello there."}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.composed_body == "Hello there."
    assert "unsubscribe" not in refreshed.composed_body.lower()
    assert "---" not in refreshed.composed_body


async def test_em_and_en_dashes_sanitised_from_body_and_subject(db_session):
    """Model-emitted em/en dashes get rewritten to ', ' before persisting."""
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text(
        '{"subject": "Quick idea \\u2014 worth a look",'
        ' "body": "Hi Jane,\\n\\nLove the pivot \\u2014 the new pricing page is sharp. '
        'Two weeks \\u2013 quick call?"}'
    ))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert "—" not in refreshed.composed_body
    assert "–" not in refreshed.composed_body
    assert "—" not in refreshed.composed_subject
    assert refreshed.composed_subject == "Quick idea, worth a look"
    assert "Love the pivot, the new pricing page is sharp." in refreshed.composed_body
    assert "Two weeks, quick call?" in refreshed.composed_body


def test_strip_long_dashes_helper():
    fn = compose_mod._strip_long_dashes
    assert fn("a — b") == "a, b"
    assert fn("a—b") == "a, b"
    assert fn("a – b") == "a, b"
    assert fn("multi — dashes — here") == "multi, dashes, here"
    # Plain hyphens left alone.
    assert fn("co-founder is in-house") == "co-founder is in-house"
    # No accidental ",," from a model that already uses "—,"
    assert fn("hi,— there") == "hi, there"
    assert fn("") == ""


async def test_status_transitions_to_done(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)
    assert lead.compose_status == ComposeStatus.PENDING

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    refreshed = await db_session.scalar(select(Lead).where(Lead.id == lead.id))
    await db_session.refresh(refreshed)
    assert refreshed.compose_status == ComposeStatus.DONE


# --------------------------------------------------------------------------
# Send-enqueue gating
# --------------------------------------------------------------------------


async def test_sample_lead_does_not_enqueue_send(db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.RUNNING)
    lead = await _make_lead(db_session, campaign, is_sample=True)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay") as enqueue:
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["status"] == "done"
    assert result["send_enqueued"] is False
    enqueue.assert_not_called()


async def test_non_sample_on_running_campaign_enqueues_send(db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.RUNNING)
    lead = await _make_lead(db_session, campaign, is_sample=False)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay") as enqueue:
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["send_enqueued"] is True
    enqueue.assert_called_once_with(str(lead.id))


async def test_non_sample_on_previewing_campaign_does_not_send(db_session):
    campaign = await _make_campaign(db_session, status=CampaignStatus.PREVIEWING)
    lead = await _make_lead(db_session, campaign, is_sample=False)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay") as enqueue:
        result = await compose_mod.compose_lead_async(str(lead.id))

    assert result["send_enqueued"] is False
    enqueue.assert_not_called()


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


async def test_missing_lead_returns_not_found():
    with patch.object(compose_mod.send_lead, "delay") as enqueue:
        result = await compose_mod.compose_lead_async(str(uuid.uuid4()))
    assert result == {"status": "not_found"}
    enqueue.assert_not_called()


async def test_calls_anthropic_with_max_tokens_1000_and_no_tools(db_session):
    campaign = await _make_campaign(db_session)
    lead = await _make_lead(db_session, campaign)

    create_mock = AsyncMock(return_value=_anthropic_text('{"subject": "S", "body": "B"}'))
    with _patch_create(create_mock), patch.object(compose_mod.send_lead, "delay"):
        await compose_mod.compose_lead_async(str(lead.id))

    kwargs = create_mock.call_args.kwargs
    assert kwargs["max_tokens"] == 1000
    # Spec: compose tasks use no tools.
    assert "tools" not in kwargs
