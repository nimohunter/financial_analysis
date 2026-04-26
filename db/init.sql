-- Create read-write application user (if not already the superuser)
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'finagent') THEN
    CREATE USER finagent WITH PASSWORD 'password';
  END IF;
END$$;

-- Create read-only user for chat backend
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'readonly') THEN
    CREATE USER readonly WITH PASSWORD 'password';
  END IF;
END$$;

-- Grant finagent full access to the database
GRANT ALL PRIVILEGES ON DATABASE finagent TO finagent;

-- readonly gets connect but only SELECT — granted after migrations run
-- (Tables don't exist yet; a post-migrate hook grants them via Alembic)
GRANT CONNECT ON DATABASE finagent TO readonly;
