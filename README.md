# Bifrost  (v2.5.0)

Bifrost is SNMP-based traffic monitoring for edge, peering and transit networks. It polls interface
counters every 5 minutes, stores the raw counters in PostgreSQL with TimescaleDB, and computes rates
when a graph is drawn. It shows how much traffic you exchange and with whom, where the network is
congested, how it is connected logically (OSPF), and the highest traffic reached. It runs in three
Docker containers and needs no online service.

Each interface gets a **role** (IX, PNI, CDN, transit, peering, customer, NNI, core, distribution) and a
**counterparty**. A counterparty **with an ASN is Internet traffic**; one **without an ASN is
backbone/internal**. Interfaces not classified yet have the role `ukategorisert` (uncategorised) and
stay off the maps and the Distribution page.

The user interface is in Norwegian or English: the installation's language is set under Settings, and each
user can choose their own on My page. This README uses the English labels with the Norwegian label in
brackets, e.g. "Settings (Innstillinger)". Source code: https://git.nordfiber.no/Nordfiber/bifrost

## Quick start

You need Docker with the Compose plugin.

```bash
git clone https://git.nordfiber.no/Nordfiber/bifrost && cd bifrost
cp .env.example .env && chmod 600 .env   # the file holds passwords
# edit .env: set DB_PASSWORD now (Postgres stores it when the data volume is created)
docker compose up -d --build
docker compose logs -f poller            # watch it poll
```

Open http://localhost:8123 (the host port is `PORT` in `.env`). The services are `db` (TimescaleDB, no
published port), `webapp` (Flask) and `poller`; data lives in the `tsdata` volume. Memory limits suit a
small host (db 512M, webapp 512M, poller 128M) and can be changed in `docker-compose.yml`.

## First login

1. Go to **/admin/login**. The normal login page links to it ("Admin-innlogging").
2. Log in with `ADMIN_PASSWORD` from `.env`, or `change-me-soon` if you did not set it. With that
   default, Bifrost makes you choose a new password (at least 12 characters) before anything else.
3. Add the first router under **Admin → Discover via SNMP (Oppdag via SNMP)**. The first graph appears
   after two polls, about 10 minutes.

This is the **break-glass admin**: one shared password that always works. `ADMIN_PASSWORD` is read only
on the very first start; later, change the password under **Admin → Security (Sikkerhet)** or with
`manage.py set-admin-password`. Everyone else logs in at `/login` with their own account.

**No devices yet?** Set `DEMO_MODE=1`, run `docker compose up -d` and press **Admin → Load demo data
(Last inn demo-data)**: 9 demo routers, 36 hours of history, a map and synthetic traffic (empty
installations only).

## The pages

| Page (Norwegian label) | What it shows |
|---|---|
| Internet (Internett) | Overview (total, transit, everything else), per customer and per category. The total counts only traffic leaving the network (IX, PNI, CDN, transit, peering). Ranges from 36 hours to 48 months; drag in a graph to zoom. |
| Backbone | Core, distribution, NNI and transport customers as separate lines. |
| Distribution (Fordeling) | Hub and spokes: whom you exchange traffic with, per ASN or per function, now or over 1 or 24 hours. |
| Maps (Kart) | Physical maps with load per link, coloured by utilisation. Each half of a link shows its own end, so a one-sided fault is visible. Click a link for its graph. |
| Logical (Logisk) | Maps built from OSPF, one per area, plus maps you make from chosen routers. Shows OSPF cost and neighbour state per link end. |
| Peaks (Trafikk-topper) | The highest traffic per series. Opened with the "All-time ▸" button on Internet or Backbone. |
| Tools (Verktøy) | History (Historikk), Inventory (Inventar), Log (Logg), Info screens (Infoscreens), My page (Min side). |
| Admin | Hosts, discovery, NetBox, Data quality (Datakvalitet), Users & groups (Brukere & grupper), Security (Sikkerhet), Settings (Innstillinger). |
| About (Om) | Click the version number in the top bar: status of app, database and poller, and how much data is stored. |

## Access

All access is given to **groups**, never to single users. Each user is in exactly one group, and every
page and API checks access on its own. The three system groups are fixed:

| Group | Sees | Can change |
|---|---|---|
| `brukere` (users; the default) | Nothing until the group is granted read access: category graphs, the Overview and Backbone totals, single customers, Distribution, public links, single maps. | Only their own password and passkeys (My page). Never write access. |
| `operatør` (operators) | Everything except Admin, including the operator-only History, Inventory, Log and LSP/OSPF views. | Can create maps, can be given write access per map, and can edit info screens. Not hosts, interfaces or users. |
| `admin` | Everything. | Everything. |

Manage users, groups and grants under **Admin → Users & groups (Brukere & grupper)**. Map access (none,
read or write) is set per map, for physical and logical maps alike. An admin can **lock** a map
(🔒 Lås) so it is read-only for everyone except admins.

Passwords are hashed with Argon2id. Deactivating a user or setting a new password logs them out at
once. Sessions last at most 18 hours for the break-glass admin and `admin`-group members
(`ADMIN_MAX_HOURS`), 2 days for operators (`OPERATOR_MAX_DAYS`) and 7 days for users (`USER_MAX_DAYS`).
`ADMIN_IDLE_MIN` adds an idle limit for the break-glass admin only (off by default).

- **Passkeys:** users can add passkeys (Face ID, fingerprint, Windows Hello, PIN, security key) on **My
  page (Min side)** and log in with one tap. They need HTTPS (localhost works for testing).
- **Public link** (`/public/map/<id>`): a minimal, self-refreshing page for one active map. It still
  needs a login, and the group needs the "public" grant.
- **Info screen** (Tools → Infoscreens): a NOC page with its own secret link and **no login**. It rotates
  through chosen physical maps and graphs. Make a new link to revoke the old one.

## Maps

**Physical maps** show routers and links with live load. **Logical maps** are built from OSPF
(OSPF-MIB), rebuilt once a day (`OSPF_REBUILD_INTERVAL`), and show neighbours you do not monitor as
ghost nodes. In the editor you drag nodes, lock them, or let Bifrost place them:

| Button | What it does |
|---|---|
| Auto | A predictable standard layout. |
| Forbedre (Improve) | Refines the current layout. |
| Nytt (New) | Builds a new layout from scratch. |
| ⇋ / ⇅ (flip / flop) | Mirrors the whole layout east–west or north–south, locked nodes too. Click again to undo. |

Auto, Forbedre and Nytt leave locked nodes in place. **Background map… (Bakgrunnskart…)** puts your own
image or a tile snapshot behind the nodes, stored in the database so viewing works offline. GPS
positions can come from NetBox.

**Lifecycle.** A map has an **active** version (what people see), at most **one saved draft**, and the
**working copy** you edit. Edits stay in the working copy until **Save draft (Lagre utkast)** or
**Activate (Aktiver)**; leave without saving and they are gone. Activating keeps the old active map as
a version; **Versions (Versjoner) → Restore (Gjenopprett)** loads a version into the working copy.

In the map list, **Park (Parker)** takes an active map out of view and gives it the status *draft*.
**Archive (Arkiver)** hides a map and keeps it; archived maps never expire. **Restore (Gjenopprett)**
brings an archived map back as a draft. **Drafts expire:** every map with the status *draft* (parked
maps, restored archives, saved drafts, new maps never activated) is deleted after `DRAFT_EXPIRE_DAYS`
days (default 28) without activity. Archive a map to keep it without showing it.

## Data and retention

- **Rates are computed at query time** from the raw counters: `(counter − previous) × 8 / seconds`.
- **Raw 5-minute samples are kept for 90 days.** Longer ranges use hourly and daily averages
  (TimescaleDB continuous aggregates), which are kept with no time limit.
- **Peaks** are stored separately and outlive the raw data: the highest 5-minute and 1-hour value per
  series, for in, out and in+out, shown for today, week, month, year and all-time. What you see follows
  your **group's** access. They are updated daily around 12:00, or at once with `manage.py fold-peaks`.

**The rate cap.** A counter glitch, or a speed change on an aggregated interface, can give one
impossible reading: a fake spike of several Tbit/s. Bifrost shows it as a gap and keeps it out of Peaks;
raw data is not changed. A rate is impossible above interface speed × **rate cap factor** (default 7, so
a 10G link is capped at 70 Gbit/s), or above **max interface speed** (default 10 Tbit/s) when the speed
is unknown. Both are settings. A speed set by hand is never overwritten by SNMP.

**Data quality (Admin → Datakvalitet)** lists glitches below the cap, each with a diagnosis and a
recommended action (★): **Rett** (fix: use the average of the neighbouring readings), **Slett** (delete
the reading) or **La ligge** (leave it: mark as known and hide it; raw data is untouched, and **Angre**
undoes it). Peaks never go down by themselves, so an old fake record stays until you press **Rens
Topper** (purge peaks) on the same page.

## Configuration

- **`.env`**: deployment settings, read when the containers start (run `docker compose up -d` after a
  change). `.env.example` lists every variable with comments.
- **Admin → Settings (Innstillinger)**: application settings, stored in the database, no restart needed.

| `.env` variable | Default | Purpose |
|---|---|---|
| `DB_PASSWORD` | `bifrost` if unset | Database password. Set your own before the first start. |
| `ADMIN_PASSWORD` | `change-me-soon` | Break-glass admin password, first start only. |
| `SECRET_KEY` | empty = generated and stored in the database | Signs sessions. Use a fixed, long, random value in production. |
| `PORT` | 8123 | Host port for the web app. |
| `PROXY_COUNT`, `TRUSTED_PROXIES` | 1, private networks | Reverse proxy in front; see nginx below. |
| `DRAFT_EXPIRE_DAYS` | 28 | Days before an unused draft map is deleted (0 = never). |
| `RESCAN_HOURS` | any time | Window for the daily interface re-scan, e.g. `2-6` = 02:00–05:59. |

The Settings page covers, among others: our name and ASN, logo and colour theme, time zone, display
language (Norwegian or English; each user can pick their own on My page), default SNMP community, SNMP
cooldown, the rate cap, **max nodes per map** (default 100), max tiles per background snapshot, NetBox
and the Google Maps and CARTO keys. For the logical maps it has **OSPF version** (v2 by default, or v3
or both), **OSPF reference bandwidth** (default 1000 Gbit/s; used to tell automatic OSPF cost from cost
set by hand, so set your network's value) and **MPLS LSP collection** (off by default).

**Branding:** upload a logo (PNG, JPEG or WebP) and a colour theme (start from
`docs/theme.example.css`) under Settings; each token's value is checked against its type (colour, font,
length and so on), and unknown tokens or values of the wrong type are left out. The main ones are
`--bl-800` (primary), `--gr-700` (accent), `--grad` (the gradient strip under the top bar and the About
title) and `--nf` / `--nf-ink` (the "bifrost" word mark).

## Time zone

All times in the app (graphs, day boundaries, the log, the re-scan window, the daily peak fold) follow
the zone chosen under **Admin → Settings (Innstillinger)**. The choice is stored in the database, and
until one is saved, Europe/Oslo is used. Data is stored in UTC, so the zone can be changed at any time.

Set the zone in the app, not in `.env`: `docker-compose.yml` fixes the containers' `TZ` to Europe/Oslo,
so `TZ` in `.env` has no effect. The container zone is only a fallback for when the setting cannot be
read, and the containers' own logs (`docker compose logs`) are stamped in Europe/Oslo.

## Behind nginx and TLS

Put nginx in front for HTTPS; `docs/nginx.conf.example` is a working example, also for a path prefix
such as `/bifrost/`.

- **`PROXY_COUNT`**: how many proxies to trust `X-Forwarded-*` from. 1 (default) for one nginx, 2 for
  e.g. CDN → nginx, 0 if the app is exposed directly.
- **`TRUSTED_PROXIES`**: networks (comma-separated CIDR) the proxy connects from. Only for these is the
  client address (used for login rate limiting and the log) taken from `X-Forwarded-For`. The default is
  private ranges and loopback; if nginx runs on another host with an address outside them, set it (this
  replaces the default).
- **Close the app port.** Compose publishes it on all interfaces. With nginx on the same host, change
  the webapp `ports:` line to `"127.0.0.1:${PORT:-8123}:8123"`, or block the port in a firewall (ports
  published by Docker usually bypass ufw rules).
- The session cookie gets the `Secure` flag automatically over HTTPS; `COOKIE_SECURE=1` forces it.

## Backup and restore

The database holds everything, traffic data and configuration. The code is in git; `.env` is not. Run
the scripts from the repository root:

| Command | What it does |
|---|---|
| `./scripts/backup-hot.sh` | `pg_dump` of the database, no downtime → `backup/tsdb-<stamp>.dump` |
| `./scripts/backup-cold.sh` | Stops the stack briefly and archives the data volume → `backup/tsdata-<stamp>.tgz` |
| `./scripts/restore.sh all` | Restores the newest backup in `./backup` |
| `./scripts/restore.sh data <file>` | Restores one file; `.tgz` or `.dump` decides the method |

Each backup also saves `.env` as `backup/env-<stamp>.bak`; the newest 10 sets are kept.

**Encryption.** Backups are encrypted by default (`openssl`, AES-256 with PBKDF2) and end in `.enc`.
Give the passphrase at the hidden prompt, or in `BACKUP_PASSPHRASE` for cron. It is never stored, so
**keep it safe**: without it the backup cannot be restored. For an unencrypted backup, press Enter at
the prompt and confirm with `j`, or set `BACKUP_PLAINTEXT=1` for cron (a cron run with neither fails on
purpose). `restore.sh` restores unencrypted files without asking for a passphrase.

**Restoring** overwrites all data: type `JA` (Norwegian for "yes") to confirm, or set `FORCE=1`. The file
is checked first, so a wrong passphrase or a damaged file stops cleanly with the data intact. A cold
(`.tgz`) restore needs the same `DB_PASSWORD` as the old server.

**Moving to a new server:** take a backup on the old server, then on the new one (same Bifrost version):

```bash
git clone https://git.nordfiber.no/Nordfiber/bifrost && cd bifrost
git checkout <same version as the old server>
mkdir -p backup && scp 'old-server:/path/to/bifrost/backup/*' backup/
# restore.sh does not restore .env; decrypt the copy first (it asks for the passphrase):
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -in backup/env-<stamp>.bak.enc -out .env
#   unencrypted backup instead:  cp backup/env-<stamp>.bak .env
chmod 600 .env
docker compose up -d --build && ./scripts/restore.sh all
curl localhost:8123/healthz
```

## Upgrading

Run `git pull && docker compose up -d --build`. The `--build` is needed every time, because the code is
built into the images. Data in the `tsdata` volume survives; only `docker compose down -v` deletes it. Database changes are applied at start and are safe
to repeat. Before moving to a **new TimescaleDB image**, take a backup: Bifrost updates the extension
itself, and that cannot be undone. If `CHANGELOG.md` says peak scoping has changed, run
`manage.py fold-peaks --reseed` once; it fills in from stored history and deletes nothing.

## Maintenance and command line

Run commands inside the webapp container: `docker compose exec webapp python manage.py <command>`

| Command | What it does |
|---|---|
| `set-admin-password` | Sets the break-glass admin password (hidden prompt); logs out all admin sessions. |
| `reset-user-password <user>` | Sets a new password for a user (hidden prompt); logs out their sessions. |
| `list-users` | Lists users, their group, and whether they are deactivated. |
| `refresh-ifalias` | Re-scans all hosts now: interface names, descriptions (`ifAlias`) and speeds. |
| `ospf-build` | Builds the logical OSPF maps now. |
| `fold-peaks [--reseed]` | Updates Peaks now; `--reseed` goes through the full history. |
| `seed-demo` | Loads the demo data (empty installation only). |

Housekeeping runs by itself: abandoned working copies go after 60 minutes, unused draft maps after 28
days, log entries after 180 days (`LOG_RETENTION_DAYS`) and finished background jobs after 30 days.
When something is wrong, look at **Tools → Log (Logg)** first; it includes database outages. If the
database goes down, the web app shows a "database unavailable" page and recovers by itself.

## SNMP

- **Bifrost only reads.** It uses SNMP GET and WALK and never writes (no SET anywhere), so it only
  needs read-only access: a read-only community for v1/v2c, or a v3 user with a read-only view. Never
  give it write access; whether a v3 user can write is decided by the access setup on the device, not
  by Bifrost, so granting write access is a configuration mistake on the device side.
- v1, v2c and v3. For v3: SHA, SHA256, SHA512 or MD5 authentication; AES, AES256 or DES privacy.
- v2c and v3 use 64-bit counters (`ifHCInOctets`/`ifHCOutOctets`). v1 cannot, so it falls back to
  32-bit counters, which wrap above about 115 Mbit/s average. Use v1 only on slow links.
- The host address must be an **IP address** (IPv4 or IPv6), not a host name. When discovering a
  device you can search NetBox by name to fill in the IP.
- Webapp and poller must reach the devices on UDP port 161. An empty community means the default from
  Settings (`public` unless changed), which is then stored on the host.
- On-demand SNMP (re-scans, map builds) queries the same router at most once per SNMP cooldown (default
  1 minute); scheduled polling is not affected. Interfaces are followed by name (`ifName`), and a
  rebooted device is re-scanned automatically.

## Security

Bifrost is built on the premise that it does not handle truly sensitive data. The database publishes
no port. SNMP credentials are stored in it in plain text, on purpose: the poller needs them in plain
text, and a key on the same disk would add nothing. They are read-only credentials, since Bifrost never
writes over SNMP (see SNMP above). Protect the surroundings instead: an encrypted disk
(LUKS or similar) under the Docker volume, `chmod 600 .env`, and encrypted backups for anything that
leaves the server. Every change needs a CSRF token, login attempts are rate-limited, and only hashed
session IDs are stored. Use TLS and a fixed `SECRET_KEY` when the app can be reached from outside.

## Development

Run the database in Docker and the app and poller locally:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml   # db on 127.0.0.1:5433
docker compose up -d db
python -m venv .venv && . .venv/bin/activate
pip install -r webapp/requirements.txt -r poller/requirements.txt
export DATABASE_URL="postgresql://bifrost:<your DB_PASSWORD>@localhost:5433/bifrost"
python webapp/app.py              # http://localhost:8123
python poller/poller.py           # in a second terminal, same DATABASE_URL
```

CI (Gitea Actions, `.gitea/workflows/`) runs syntax checks, unit tests and integration tests on every
push to `review` and `main`.

## Documentation and examples

The files in `docs/` explain Bifrost, and they are also meant as **examples of how things can be done**
— in Bifrost and in other projects. Other pages link to them (the GUI recipe in particular), so their
names stay the same.

| File | What it is |
|---|---|
| [`docs/Home.md`](docs/Home.md) | The overview: what Bifrost is, the pages, maps, access and data, in plain language. It is also the wiki's front page. |
| [`docs/bifrost-gui-oppskrift.md`](docs/bifrost-gui-oppskrift.md) | A recipe for the calm dashboard style used in Bifrost: the principles, design tokens, cards, segmented controls and uPlot charts. Written to be reused in other projects. Also on the wiki. |
| [`docs/theme.example.css`](docs/theme.example.css) | A complete colour theme to copy, change and upload (see Branding above). |
| [`docs/nginx.conf.example`](docs/nginx.conf.example) | nginx terminating HTTPS in front of Bifrost, at the root or under a path prefix. |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each version. |

## Author

Roger Jorgensen — <rogerj@gmail.com>

Design, architecture and structure; the data model, and the choice to store the raw counters and work
out rates when they are read; how traffic is combined and presented, in particular the Distribution page
and the automatic generation and placement of network maps; and the decisions about what Bifrost does
and deliberately does not do: one database, no online services, a fixed five-minute poll, and nothing
destructive.

The code is written by Claude AI (Opus and Fable).

Reviewed independently by Qwen, DeepSeek, Fable, Opus, Gemini and MiMo.

## License

Bifrost is **dual-licensed**:

- **Open source:** [GNU AGPL v3 or later](LICENSE) (`AGPL-3.0-or-later`). You may use, change and share
  Bifrost freely. If you offer a **modified** version to others, including as a network service, the
  changes must be available under the same license. Running Bifrost internally for your own network
  carries no such obligation.
- **Commercial license** without the AGPL obligations: available on request from <rogerj@gmail.com>.

© Roger Jorgensen. The design, the data model and the way traffic is combined and presented, in
particular the Distribution page and the automatic generation and placement of network maps, are
developed in-house. Every source file carries `SPDX-License-Identifier: AGPL-3.0-or-later`.

Third-party components keep their own licenses. [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
lists all of them — the files under `webapp/static/vendor/`, the Python packages, the container images
and the map tile services — with the license each is under. Files under `webapp/static/vendor/` are
third-party, carry their license text next to them and are not marked with Bifrost's license.
