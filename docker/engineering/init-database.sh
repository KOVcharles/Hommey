#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 <<'SQL'
\getenv ai_password HOMMEY_AI_DB_PASSWORD
SELECT format('CREATE ROLE hommey_ai LOGIN PASSWORD %L', :'ai_password') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
