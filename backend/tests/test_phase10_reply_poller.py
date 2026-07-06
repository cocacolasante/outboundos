"""Phase 10: reply_poller orchestration (imaplib mocked)."""
from datetime import datetime, time, timezone
from unittest.mock import patch

from sqlalchemy import select

from app.models import (
    Campaign,
    CampaignStatus,
    ConnectedAccount,
    ConnectedAccountTestStatus,
    EmailEvent,
    EmailEventType,
    Lead,
)
from app.services import encryption
from app.workers.reply_poller import poll_account_for_replies, poll_all_replies_async


async def _make_account(
    db_session, *,
    label: str = "Work Gmail",
    email: str = "me@gmail.com",
    status: ConnectedAccountTestStatus = ConnectedAccountTestStatus.OK,
) -> ConnectedAccount:
    acc = ConnectedAccount(
        label=label, email_address=email,
        imap_host="imap.gmail.com", username=email,
        password_encrypted=encryption.encrypt("app-password"),
        last_test_status=status,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


async def _make_campaign(
    db_session, account_id, *,
    status: CampaignStatus = CampaignStatus.RUNNING,
) -> Campaign:
    c = Campaign(
        name="P10",
        goal="g", tone="t",
        sender_name="s", sender_email="s@x.com",
        sample_count=1,
        connected_account_id=account_id,
        schedule_days=[0, 1, 2, 3, 4],
        schedule_time_start=time(9, 0), schedule_time_end=time(17, 0),
        status=status,
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    return c


# --------------------------------------------------------------------------
# poll_account_for_replies
# --------------------------------------------------------------------------


async def test_poll_account_matches_reply_and_records_event(db_session):
    acc = await _make_account(db_session)
    campaign = await _make_campaign(db_session, acc.id)
    lead = Lead(
        campaign_id=campaign.id, email="lead@external.com",
        brevo_message_id="msg-orig", composed_subject="Hi",
    )
    db_session.add(lead)
    await db_session.commit()

    fake_message = {
        "uid": "10",
        "message_id": "mid-orig-reply",
        "in_reply_to": "msg-orig",
        "references": [],
        "subject": "Re: Hi",
        "from_email": "lead@external.com",
    }
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[fake_message],
    ):
        result = await poll_account_for_replies(acc, [campaign.id], db_session)
    await db_session.commit()

    assert result == {"ok": True, "replies_found": 1}

    events = (await db_session.execute(
        select(EmailEvent).where(EmailEvent.event_type == EmailEventType.REPLIED)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].lead_id == lead.id


async def test_poll_account_no_match_no_event(db_session):
    acc = await _make_account(db_session)
    campaign = await _make_campaign(db_session, acc.id)
    # Lead exists but message doesn't match it.
    db_session.add(Lead(
        campaign_id=campaign.id, email="lead@external.com",
        brevo_message_id="msg-xyz", composed_subject="Different",
    ))
    await db_session.commit()

    fake_message = {
        "uid": "1", "message_id": "mid-stranger",
        "in_reply_to": "unrelated", "references": [],
        "subject": "Re: something else", "from_email": "stranger@x.com",
    }
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[fake_message],
    ):
        result = await poll_account_for_replies(acc, [campaign.id], db_session)

    assert result == {"ok": True, "replies_found": 0}


async def test_poll_account_imap_failure_marks_account_failed(db_session):
    acc = await _make_account(db_session)
    campaign = await _make_campaign(db_session, acc.id)

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        side_effect=Exception("auth failed"),
    ):
        result = await poll_account_for_replies(acc, [campaign.id], db_session)

    assert result["ok"] is False
    assert "auth failed" in result["error"]
    # In-memory ORM state reflects the failure.
    assert acc.last_test_status == ConnectedAccountTestStatus.FAILED
    assert acc.last_test_error == "auth failed"


async def test_poll_account_updates_last_polled_at_on_success(db_session):
    acc = await _make_account(db_session)
    campaign = await _make_campaign(db_session, acc.id)
    assert acc.last_polled_at is None

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[],
    ):
        await poll_account_for_replies(acc, [campaign.id], db_session)

    assert acc.last_polled_at is not None
    assert acc.last_polled_at.tzinfo is not None


async def test_poll_account_records_processed_message_ids(db_session):
    """Every fetched message's Message-ID is appended to the account's
    processed list (regardless of whether it matched a lead) so the next
    poll cycle short-circuits and doesn't re-create a REPLIED event."""
    acc = await _make_account(db_session)
    campaign = await _make_campaign(db_session, acc.id)
    db_session.add(Lead(
        campaign_id=campaign.id, email="lead@external.com",
        brevo_message_id="msg-X", composed_subject="Hi",
    ))
    await db_session.commit()

    messages = [
        {"uid": "1", "message_id": "mid-matched",
         "in_reply_to": "msg-X", "references": [],
         "subject": "Re: Hi", "from_email": "lead@external.com"},
        {"uid": "2", "message_id": "mid-unmatched",
         "in_reply_to": "", "references": [],
         "subject": "Newsletter", "from_email": "list@elsewhere.com"},
    ]
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=messages,
    ):
        result = await poll_account_for_replies(acc, [campaign.id], db_session)
    await db_session.commit()

    assert result == {"ok": True, "replies_found": 1}
    # Both Message-IDs are remembered — even the unmatched one — so we
    # don't re-fetch and re-parse it forever.
    assert "mid-matched" in acc.processed_imap_message_ids
    assert "mid-unmatched" in acc.processed_imap_message_ids


async def test_poll_account_passes_processed_set_to_imap_client(db_session):
    """The previously-recorded Message-IDs flow back to the IMAP
    fetcher as the dedup set, so messages stay unread for the user but
    we don't reprocess them."""
    acc = await _make_account(db_session)
    acc.processed_imap_message_ids = ["old-id-1", "old-id-2"]
    await db_session.commit()
    await _make_campaign(db_session, acc.id)

    captured: dict = {}

    def fake(account, since, processed):
        captured["processed"] = processed
        return []

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        side_effect=fake,
    ):
        await poll_account_for_replies(acc, [], db_session)

    assert captured["processed"] == {"old-id-1", "old-id-2"}


async def test_poll_account_trims_processed_ids_to_max(db_session):
    """The processed list is bounded so the JSON column doesn't grow
    unbounded across years of polling."""
    from app.workers.reply_poller import MAX_PROCESSED_IDS

    acc = await _make_account(db_session)
    # Pre-fill the list well past the cap.
    acc.processed_imap_message_ids = [f"old-{i}" for i in range(MAX_PROCESSED_IDS + 50)]
    await db_session.commit()
    await _make_campaign(db_session, acc.id)

    fresh = [
        {"uid": str(i), "message_id": f"new-{i}",
         "in_reply_to": "", "references": [],
         "subject": "", "from_email": ""}
        for i in range(10)
    ]
    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=fresh,
    ):
        await poll_account_for_replies(acc, [], db_session)

    ids = acc.processed_imap_message_ids
    assert len(ids) == MAX_PROCESSED_IDS
    # The most recent additions survive; oldest get trimmed.
    assert "new-9" in ids
    assert "old-0" not in ids


# --------------------------------------------------------------------------
# poll_all_replies_async (orchestration)
# --------------------------------------------------------------------------


async def test_poll_all_replies_skips_failed_accounts(db_session):
    ok_acc = await _make_account(db_session, label="OK", email="ok@x.com",
                                  status=ConnectedAccountTestStatus.OK)
    failed_acc = await _make_account(db_session, label="Broken", email="broken@x.com",
                                      status=ConnectedAccountTestStatus.FAILED)
    await _make_campaign(db_session, ok_acc.id)
    await _make_campaign(db_session, failed_acc.id)

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[],
    ) as fetch:
        result = await poll_all_replies_async()

    assert result["accounts_checked"] == 1  # only the OK account
    assert fetch.call_count == 1


async def test_poll_all_replies_skips_account_with_no_active_campaigns(db_session):
    acc = await _make_account(db_session, email="solo@x.com")
    # Make a draft campaign — not in (running, paused, complete)
    await _make_campaign(db_session, acc.id, status=CampaignStatus.DRAFT)

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[],
    ) as fetch:
        result = await poll_all_replies_async()

    fetch.assert_not_called()
    assert result["replies_found"] == 0


async def test_poll_all_replies_includes_paused_and_complete_campaigns(db_session):
    acc = await _make_account(db_session, email="active@x.com")
    await _make_campaign(db_session, acc.id, status=CampaignStatus.PAUSED)
    await _make_campaign(db_session, acc.id, status=CampaignStatus.COMPLETE)

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        return_value=[],
    ) as fetch:
        result = await poll_all_replies_async()

    fetch.assert_called_once()
    assert result["accounts_checked"] == 1


async def test_poll_all_replies_aggregates_counts_across_accounts(db_session):
    acc1 = await _make_account(db_session, email="one@x.com")
    acc2 = await _make_account(db_session, email="two@x.com")
    c1 = await _make_campaign(db_session, acc1.id)
    c2 = await _make_campaign(db_session, acc2.id)

    lead1 = Lead(campaign_id=c1.id, email="r1@y.com", brevo_message_id="msg-1", composed_subject="A")
    lead2 = Lead(campaign_id=c2.id, email="r2@y.com", brevo_message_id="msg-2", composed_subject="B")
    db_session.add_all([lead1, lead2])
    await db_session.commit()

    def fake_fetch(account, since, processed):
        if account.username == "one@x.com":
            return [{"uid": "1", "message_id": "id-1",
                     "in_reply_to": "msg-1", "references": [],
                     "subject": "Re: A", "from_email": "r1@y.com"}]
        return [{"uid": "2", "message_id": "id-2",
                 "in_reply_to": "msg-2", "references": [],
                 "subject": "Re: B", "from_email": "r2@y.com"}]

    with patch(
        "app.workers.reply_poller.imap_client.fetch_recent_with_account",
        side_effect=fake_fetch,
    ):
        result = await poll_all_replies_async()

    assert result["replies_found"] == 2
    assert result["accounts_checked"] == 2
