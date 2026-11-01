# Development

How Bifrost's code is laid out, and the rules a change has to follow. Many of the rules are enforced by
tests: the guard named under a rule fails when the rule is broken. How to run Bifrost locally is in the
README under [Development](README.md#development).

## Layout

| Path | What it is |
|---|---|
| `poller/poller.py` | Reads the interface inventory from the database, polls in/out counters every 300 seconds and writes the raw counters. Never computes rates |
| `webapp/` | The Flask application (served by gunicorn) |
| `webapp/templates/` | Jinja templates |
| `webapp/static/` | Bifrost's own JavaScript and the single stylesheet `app.css` |
| `webapp/static/vendor/` | Vendored libraries (htmx, uPlot, Leaflet, Inter), each with its license text. Not modified |
| `webapp/lang/` | Language packs: `no.json` (the reference) and `en.json` |
| `webapp/tests/` | `test_*.py` unit tests, `citest_*.py` integration tests (need a database), `livecheck_mapsources.py` |
| `db/schema.sql` | Initializes a fresh data volume (the `if_sample` table only); everything else is created at startup |
| `scripts/` | Backup and restore |
| `.gitea/workflows/` | CI |

The modules in `webapp/`:

| Module | Responsibility |
|---|---|
| `app.py` | Main routes, the request gate (`_gate`: CSRF, session, setup, time limits), CSP and security headers, `__version__` |
| `db.py` | Connection pool, schema and migrations at startup, boot gate, TimescaleDB policies, logging to the database |
| `queries.py` | Rate queries (rates computed at query time), customer traffic, peaks |
| `catalog.py`, `classmap.py` | The category and graph grouping, defined in one place and used everywhere; the mapping from interface descriptions (ifAlias/ifName) to name, role and ASN |
| `acl.py` | The central access register and gate |
| `maps.py`, `ospf.py`, `placement.py` | Physical maps, logical (OSPF) maps, the placement engine |
| `geo.py` | Web Mercator projection and server-side stitching of map tiles into a background snapshot |
| `payload.py` | Validation of everything the map editors send |
| `infoscreen.py` | Info screens: named kiosks that rotate through slides on an unguessable token URL, without login |
| `snmp.py`, `inventory_ops.py`, `netbox.py` | SNMP discovery, background jobs (rescan, NetBox sync), NetBox integration |
| `settings.py`, `i18n.py`, `branding.py`, `mapsources.py` | Settings, language packs, logo and theme, map tile sources |
| `auth.py`, `sessions.py`, `passkeys.py` | Passwords, server-side sessions, passkeys |
| `manage.py` | The command line, run inside the webapp container |
| `demo_data.py` | The demo data set (a nine-device backbone with synthetic history) |
| `layout_playground.py` | A stand-alone workshop for tuning the placement engine; writes SVG files and needs neither the app nor the database |

## Fixed design constraints

- **Bifrost only reads over SNMP** (GET and WALK, never SET).
- **Raw counters are stored; rates are computed at query time.** Raw data is never downsampled destructively;
  the aggregates come in addition.
- **No dependency on external services at runtime.** The only deliberate fetch is map tiles.
- **Nothing destructive:** everything must survive being run again (idempotent, upsert).

## Pages and front end

**No inline style.** The Content-Security-Policy is `style-src 'self' 'nonce-…'` without `'unsafe-inline'`.
- No `style=""` in templates, and no `style` attribute set from JavaScript — including attribute maps for SVG
  and HTML strings. Use classes in `app.css`; the `bf-` classes are utility classes (one declaration,
  `!important`). Setting `el.style.x` (CSSOM) is allowed.
- Elements that start hidden use the `hidden` attribute. `[hidden]{display:none!important}` stays last in
  `app.css`.
- Guard: `test_no_inline_style`, `citest_csp_pages`.

**Nonces.** Every `<style>` block and inline `<script>` carries `nonce="{{ csp_nonce }}"`. Data for scripts goes
in `<script type="application/json">`, written with `|tojson`. Guard: `citest_csp_pages`.

**Text.** Every string in the interface is a key in both `lang/no.json` and `lang/en.json`, with the same
placeholders; counted keys come in `.one`/`.other` pairs. Guard: `test_i18n`.

**Theme.** A new CSS token is added to `branding.TOKEN_TYPES` with its type. Guard: `test_branding`.

**One view for all roles.** Every role sees the same information and layout; only buttons and actions differ.
Reuse the templates with the `read_only`/`can_*` flags — no simplified copies.

## Input and access

**Every route and API checks access itself,** for each part of the response (per series, per object), also
when parameters are left out. Never rely on the interface to send the right parameters. The central gate is
`acl.can(perm, object_type, object_id)`; graph scope comes from `acl.scope()`. Account data (`/account/*`) is
always tied to the session's user, never to an id sent by the client. Guard: `citest_acl_gating`,
`citest_map_gating`, `citest_account_ownership`.

**CSRF.** State-changing requests need the token: htmx sends the `X-CSRFToken` header, forms carry a
`csrf_token` field. The request gate in `app.py` enforces it. Guard: `citest_input_guards`.

**Map editor input** is validated in full in `payload.py` before anything is written. A rejection is a 400 and
a log line (`payload.reject`). Guard: `citest_map_input`.

## Errors and logging

**The application log is the operator's first diagnosis.** Every record on the root logger is also written to
the `log_event` table (the Log page) by `db.DbLogHandler`. Write messages for the operator: what happened, why,
and what to do. High-frequency progress lines are marked `extra={"skip_db": True}`.

**No silent `except`.** Database errors (`psycopg2.Error`) propagate. Anything else that is caught broadly is
logged — `log.exception` when it is unexpected, a warning for an expected fallback — and the line is marked
`# noqa: BLE001 - <reason>`.

**Repeating warnings** (a fallback that would otherwise log on every request) go through
`db.warn_throttled(logger, key, msg)`.

## Database

- `db/schema.sql` only initializes a fresh volume. All other schema is created and migrated at startup in
  `db.py` (`bring_up` → `ensure_pg_config`, `ensure_metrics_schema`, `ensure_metrics_aggregates`), behind the
  boot gate, so it runs once before the workers and the poller start.
- Migrations are additive and idempotent (`IF NOT EXISTS`, upsert) and must survive running again.
- Continuous aggregates and policies are created on an autocommit connection (`db.autocommit_conn()`), not in a
  transaction.

## Map sources

A new tile source goes into `mapsources.SOURCES` (url, attribution, `max_zoom`, `snapshot_ok`, `live_ok`, and
`key`/`key_param`/`session` when needed). Read the source's terms of use first, and add the source to `EXPECTED`
in `tests/livecheck_mapsources.py`. Guard: `test_mapsources`, `livecheck_mapsources`.

## Dependencies

Python packages are pinned exactly, including indirect ones, in `webapp/requirements.txt` and
`poller/requirements.txt`; images are pinned in the Dockerfiles and `docker-compose.yml`. A new package, vendored
file or image gets a row in `THIRD_PARTY_LICENSES.md`, with the license read at the source. Guard:
`test_third_party`.

## Language, changelog and commits

- Everything in the repository is in English: code and names, comments and docstrings, README, `docs/`,
  `CHANGELOG.md` and commit messages. The text in the user interface comes from the language packs in
  `webapp/lang/`.
- `CHANGELOG.md` is technically correct and describes the result for the user or operator. It refers to nothing
  outside Bifrost: no case or issue numbers, reviews or other systems.
- The developer approves every changelog entry and every commit before it is made.

## Files, versions and comments

- Source files start with `Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: vX.Y.Z` and
  `SPDX-License-Identifier: AGPL-3.0-or-later`, in the file type's comment syntax. "Last modified" changes only
  with a real change to the file. Not in `static/vendor/`, Markdown or JSON.
- `__version__` in `webapp/app.py` is the only version source; the footer, the About page, `/healthz` and the
  User-Agent toward tile servers and NetBox read it.
- New and changed comments are in English, generic, and say what and why. They contain no personal or
  sensitive information — no names, e-mail addresses or anything else that identifies who reported something.
- No status ("tested", "pending" and the like) in code, comments or documentation.

## Tests

- Unit tests: `python -m unittest discover -s tests` from `webapp/`. A few need the repository root and are
  skipped inside the container image; run them from a checkout as well.
- Integration tests (`citest_*.py`) need a running database and are not picked up by `unittest discover`. Run
  each one as a script inside the webapp container once the database is up, for example
  `docker compose exec webapp python tests/citest_input_guards.py`. CI runs them all in
  `.gitea/workflows/ci-integration.yml`.
- An integration test cleans up the data it creates.
