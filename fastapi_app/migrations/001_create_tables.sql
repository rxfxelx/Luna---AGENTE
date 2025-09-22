-- SQL migration for the initial Luna database schema.
-- This script creates the tables used to persist users and
-- messages.  It can be applied manually using psql or a migration
-- tool such as Alembic.  When running locally you may instead
-- rely on ``init_models`` in ``fastapi_app.db`` to create tables.

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    phone VARCHAR(30) NOT NULL UNIQUE,
    name VARCHAR(255),
    thread_id VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sender VARCHAR(10) NOT NULL,
    content TEXT,
    media_type VARCHAR(20),
    media_url TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);