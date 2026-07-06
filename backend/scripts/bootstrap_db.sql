-- Multi-tenancy Phase 2: runtime DB role bootstrap.
--
-- Creates the non-owner `app_user` role the runtime connects as once RLS
-- enforcement lands (enforcement phase): table owners BYPASS row-level
-- security, so policies only bite when the app stops connecting as the
-- owner.  Idempotent — safe to re-run (re-running also re-asserts the
-- NOBYPASSRLS attributes and re-grants).
--
-- Usage (dev):
--   docker compose exec -T postgres psql -U emailblaster -d emailblaster \
--     -v app_password="'CHANGE-ME'" -f - < backend/scripts/bootstrap_db.sql
-- Then set APP_DATABASE_URL in .env, e.g.
--   APP_DATABASE_URL=postgresql+asyncpg://app_user:CHANGE-ME@postgres:5432/emailblaster
--
-- Deliberately NOT an Alembic migration: role passwords don't belong in
-- migration history, and role management is instance-level, not schema.

SELECT set_config('bootstrap.app_password', :app_password, false);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    EXECUTE format(
      'CREATE ROLE app_user LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
      current_setting('bootstrap.app_password')
    );
  ELSE
    EXECUTE format(
      'ALTER ROLE app_user LOGIN PASSWORD %L',
      current_setting('bootstrap.app_password')
    );
  END IF;
END
$$;

-- Re-assert the RLS-critical attributes even if the role pre-existed.
ALTER ROLE app_user NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;

-- Future tables created by the owner (Alembic migrations) stay usable.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
