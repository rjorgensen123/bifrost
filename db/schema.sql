-- Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.2.4
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- ============================================================================
--  bifrost  -  TimescaleDB schema (METRICS ONLY)
--  Raw 64-bit SNMP counters. Config (hosts/interfaces/auth) ligger i SAMME
--  Postgres-instans (R-016: SQLite-omtalen var foreldet — config er Postgres,
--  se webapp/db.py:CONFIG_SCHEMA_PG). if_sample er keyet på interface_id
--  (= interface.id i config-skjemaet) uten FK.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS if_sample (
    time         timestamptz NOT NULL,
    -- NB (R-016): int her vs BIGINT IDENTITY på interface.id — bevisst, dokumentert
    -- antakelse om at interface.id aldri passerer 2^31 (queries.py caster ::int[]).
    interface_id int         NOT NULL,   -- = interface.id i config-skjemaet (samme Postgres)
    in_octets    bigint      NOT NULL,   -- ifHCInOctets  (Counter64)
    out_octets   bigint      NOT NULL    -- ifHCOutOctets (Counter64)
);

SELECT create_hypertable('if_sample', 'time', if_not_exists => TRUE);
-- G-310: UNIK indeks på (interface_id, time) hindrer duplikat-sampler (poller-retry / dobbeltstartet
-- poller / delvis restore+repoll) som ellers dobbelttalte deltaer/rater. Inkluderer partisjonskolonnen
-- `time` (påkrevd for unik indeks på en hypertable) og dekker også (interface_id, time DESC)-spørringene.
CREATE UNIQUE INDEX IF NOT EXISTS if_sample_iface_time_uq
    ON if_sample (interface_id, time DESC);

-- Kompresjon (14 d) + retention (rå 90 d) + continuous aggregates AKTIVERES av
-- webappen ved oppstart: webapp/db.py:ensure_metrics_aggregates (G-26).
-- (R-016: tidligere «OPTIONAL/utkommentert»-kommentar her var misvisende —
-- policyene er i drift, styrt fra db.py, ikke fra denne fila.)
