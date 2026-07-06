"""Phase 2: verify the Alembic migration applies and rolls back on a fresh DB."""
import os
import subprocess

import asyncpg
import pytest

MIGRATION_DB = "emailblaster_migration_test"
ADMIN_DSN = "postgresql://emailblaster:emailblaster@postgres:5432/postgres"
MIGRATION_DSN_ASYNCPG = f"postgresql+asyncpg://emailblaster:emailblaster@postgres:5432/{MIGRATION_DB}"


async def _admin_exec(sql: str) -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture
async def fresh_migration_db():
    await _admin_exec(
        f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)'
    )
    await _admin_exec(f'CREATE DATABASE "{MIGRATION_DB}"')
    yield
    await _admin_exec(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)')


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": MIGRATION_DSN_ASYNCPG}
    return subprocess.run(
        ["alembic", *args],
        cwd="/app",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_alembic_upgrade_head_creates_all_tables(fresh_migration_db):
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic upgrade failed:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"

    conn = await asyncpg.connect(
        f"postgresql://emailblaster:emailblaster@postgres:5432/{MIGRATION_DB}"
    )
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
        names = {r["tablename"] for r in rows}
        expected = {
            "connected_accounts",
            "campaigns",
            "leads",
            "email_events",
            "suppression_list",
            "style_corrections",
            "alembic_version",
        }
        assert expected <= names, f"Missing tables: {expected - names}"

        # Spot-check a few indexes from the spec.
        idx_rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
        )
        idx = {r["indexname"] for r in idx_rows}
        for required in (
            "ix_leads_campaign_id",
            "ix_leads_send_status",
            "ix_leads_email",
            "ix_email_events_campaign_id",
            "ix_email_events_lead_id",
            "ix_suppression_list_email",
            "ix_connected_accounts_email_address",
        ):
            assert required in idx, f"Missing index after upgrade: {required}"
    finally:
        await conn.close()


async def test_alembic_downgrade_to_base_drops_all_tables(fresh_migration_db):
    up = _run_alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _run_alembic("downgrade", "base")
    assert down.returncode == 0, f"alembic downgrade failed:\nSTDOUT:{down.stdout}\nSTDERR:{down.stderr}"

    conn = await asyncpg.connect(
        f"postgresql://emailblaster:emailblaster@postgres:5432/{MIGRATION_DB}"
    )
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        )
        names = {r["tablename"] for r in rows}
        # Only alembic_version should remain (or nothing).
        leftover = names - {"alembic_version"}
        assert not leftover, f"Tables left after downgrade: {leftover}"
    finally:
        await conn.close()
