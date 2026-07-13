--  Postgres schema for sdr-trunk-vtt (calls table).
-- Create a dedicated database + role first, e.g.:
--   CREATE USER vtt WITH PASSWORD 'choose-a-strong-password';
--   CREATE DATABASE vtt OWNER vtt;
-- Then connect to database `vtt` and run this file.
--
-- Tables live in schema "trunk-recorder-oltp" (not public).
-- The API sets search_path via DATABASE_SCHEMA (default matches below).

CREATE SCHEMA IF NOT EXISTS "trunk-recorder-oltp";
SET search_path TO "trunk-recorder-oltp";

CREATE TABLE IF NOT EXISTS calls (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL,
    system_name     TEXT,
    talkgroup       INTEGER,
    talkgroup_tag   TEXT,
    src             INTEGER,
    src_tag         TEXT,
    freq            DOUBLE PRECISION,
    call_length     DOUBLE PRECISION,
    wav_path        TEXT NOT NULL DEFAULT '',
    json_path       TEXT,
    metadata_json   TEXT,
    transcript      TEXT,
    backend_used    TEXT,
    error_message   TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    has_alert       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_calls_status ON calls (status);
CREATE INDEX IF NOT EXISTS idx_calls_created ON calls (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_talkgroup ON calls (talkgroup);
CREATE INDEX IF NOT EXISTS idx_calls_talkgroup_created ON calls (talkgroup, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_has_alert_created ON calls (has_alert, created_at DESC);

-- Connection for the API (k8s Secret / .env):
-- DATABASE_URL=postgresql://vtt:PASSWORD@192.168.1.162:2665/vtt
-- DATABASE_SCHEMA=trunk-recorder-oltp
--
-- Ensure pg_hba.conf / firewall allow the k3s node LAN to this port.
-- Host port must map to container 5432 (e.g. 2665:5432).
