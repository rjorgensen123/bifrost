# Changelog

## 2.5.2 — Link width on the physical map shows capacity (2026-11-23)

- **Link width shows capacity on the physical map:** the width of a link follows its capacity in fixed
  steps — 2 px below 10G, 4 px from 10G, 6 px from 40G and 8 px from 100G — instead of its utilisation.
  The colour still shows utilisation, and the map subtitle says what each one means.

## 2.5.1 — Python packages updated for published vulnerabilities (2026-11-23)

- **Python packages updated:** Pillow 13.0.0 (from 12.3.0), certifi 2026.10.18 (from 2026.7.22), pyasn1
  0.6.5 (from 0.6.4, in both the web application and the poller), requests 2.33.0 (from 2.32.3), Werkzeug
  3.1.9 (from 3.1.8) and Flask 3.1.3 (from 3.1.0). These versions fix published vulnerabilities in the
  packages. Bifrost behaves as before.

## 2.5.0 — The repository starts over (2026-11-01)

### Note — the history starts here, on purpose

The repository Bifrost lived in was cleaned out and started fresh at this version. The earlier commits
were written while Bifrost was being built for internal use, and they describe that work: internal
documents, case numbers from a tracker nobody outside can reach, and reviews. 603 of them also carried an
author identity that is no longer the right one, and their messages were largely in a language Bifrost
has since stopped using. Carrying them along would have mixed internal history into a project meant to
stand on its own.

What each release changed is in this file. The code is what it was — 2.5.0 starts from the tree that was
there at 2.4.3, and what it changes on top of that is listed below. Only the commit history was left
behind, and the untouched original is archived where the people who need it can reach it.

### Note — the earlier entries were cleaned up here, once

The entries for the releases below were cleaned up and corrected in this release, and only here. What
each release changed is the same as before. From this release on, an entry is not changed after the
release it describes.

### Changed

- **Source code on GitHub:** the repository links in the README, in the documentation and on the About
  page point to https://github.com/rjorgensen123/bifrost, and the About page's Wiki link opens the
  overview in `docs/`.
- **Demo data from the command line:** the Load demo data button is gone. `manage.py seed-demo` loads the
  demo data and activates the demo map, and the Demo card on the Admin page shows the command.
- **README and documentation** are checked against the code and corrected, and `THIRD_PARTY_LICENSES.md`
  also lists the `alpine` image that the backup and restore scripts use.

### Fixed

- **Log level colours:** on the Log page, WARNING is orange and ERROR is white on red again; the other
  levels are no longer coloured.

### Removed

- **Theme tokens `--sort` and `--ink-strong`:** nothing used them. An uploaded theme that sets them now
  lists them as ignored.

## 2.4.3 — Preparing the move to the new repository (2026-11-01)

- **User-Agent follows the version:** requests to map tile servers and to NetBox carry the running Bifrost
  version in their User-Agent (`Bifrost-netmon/<version>` and `Bifrost/<version>`) instead of the fixed
  versions 1.3 and 1.5.

## 2.4.2 — Theme and styles: no inline styles, theme values checked by type (2026-10-11)

- **No theme value in page markup:** the «down» line in the physical map's link pop-up takes its colour
  from the stylesheet instead of having the theme colour written into the HTML, and the addresses in the
  map pages' data blocks are written as escaped JSON.
- **Theme values checked by type:** an uploaded theme keeps a token only when the app uses it and its value
  has the token's type — a colour must be a valid CSS colour (or refer to another colour token), a font a
  font-family list, a length a number with a unit, the gradient a linear gradient of colours, and the RGB
  tokens three numbers from 0 to 255. Anything else is left out and named in the upload message. A stored
  token that does not pass is no longer emitted, and that is logged.
- **No inline styles in the pages:** the 227 `style=""` attributes in the templates and the style
  attributes the map scripts set on SVG elements are now CSS classes with the same values, and elements
  that start hidden use the `hidden` attribute. The pages look and behave as before.
- **Content Security Policy without inline styles:** the policy no longer allows `'unsafe-inline'` for
  styles. The style blocks that remain (the theme and the public map pages) carry the per-request nonce,
  and htmx no longer adds its own style block (its rule is in the stylesheet). A test fetches every page
  and fails on a style block or inline script without the nonce, a `style=""` attribute or a JSON data
  block that does not parse.

## 2.4.1 — Map backgrounds: CARTO API key, more open map sources, attribution on every map (2026-10-11)

- **CARTO needs an API key:** CARTO's terms require a key on every tile request and forbid storing the
  tiles on a server, and without a valid key CARTO answers with an «API KEY REQUIRED» tile. A CARTO API
  key can now be set under Admin → Settings (or `CARTO_API_KEY` in `.env`). It is checked against CARTO
  before it is saved and added to the tile URLs. Without the key CARTO cannot be chosen, and CARTO can
  only be used live. The CARTO addresses and attribution follow CARTO's current format.
- **Keys are required where a source needs one:** a background from a source whose API key is not set
  (CARTO or Google) is refused instead of being saved and then failing when the map is shown.
- **Stored CARTO backgrounds:** a map whose background is a stored CARTO image keeps it. The background
  editor says that the source's terms do not allow storing it and suggests locking the view as live.
- **More open map sources:** Kartverket topo raster and Kartverket nautical chart (Norway), and
  OpenTopoMap (global, topographic). None of them needs a key, and all can be used as snapshot or live.
- **Attribution on every map:** the background's attribution is shown on logical maps and in the map
  editor too, not only on the physical map view. Live backgrounds show the source's current attribution.

## 2.4.0 — NetBox and OSPF error handling, logging, read-only SNMP, file headers in English (2026-10-11)

- **Button names in help texts:** the map editor, the NetBox sync message and the data-quality repair name
  the buttons as they are labelled («Lagre utkast»/«Aktiver», «GPS plasser», «Slett»).
- **File headers in English:** the header line of every source file reads «Last modified» instead of «Sist
  endret», and the two CI workflows carry the same header line as the other files; the versions are unchanged.
- **NetBox sync errors are reported as errors:** when NetBox answers with an error (for example 500 or 429)
  or does not answer, the host is counted as an error with the reason instead of «not found», and the error
  is logged. The sync summary also shows how many hosts were not found and the result for the OSPF ghost
  routers.
- **Fallbacks are logged:** when the session check, the password-change check, the schema retry or the boot
  gate fails for another reason than the database being down, it is logged and the request goes on as
  before. A failed re-scan is logged, and a setting that cannot be read is logged at most once every five
  minutes while the default value is used.
- **SNMP is read-only:** Bifrost only reads over SNMP and never writes. The README, the documentation and
  the SNMP fields say so: give it read-only access only.
- **OSPF map clean-up when a host does not answer:** a host that gives no fresh data no longer stops the
  clean-up for the rest of the network. It keeps what it had: its own side of each link stays, as a half
  link when only it confirms it, and it is drawn faint on the logical map with a note. Links and ghost
  routers that the answering hosts no longer report are removed, and the hosts that did not answer are
  named in the build status.
- **Automatic placement:** «Improve» skips its swap step on maps above 70 nodes, like the other steps, so
  it no longer risks a time-out on large maps. The same placement function on the same map gives the same
  result after a restart, also on logical maps.
- **Settings with fixed choices:** a setting with a fixed list of choices (time zone, language) only accepts
  one of them, whichever way it is saved; an unknown time zone is rejected.
- **Map saves are checked in full:** what the map editors send when saving, saving a draft, activating or
  placing objects automatically is checked before anything is written, on physical and logical maps. A
  value of the wrong type, a node or link that is not on the map, or an invalid setting is rejected with a
  message saying what was wrong, nothing is saved, and the rejection is logged.
- **Text from routers with æøå:** names and descriptions read over SNMP come out right whether the router
  sends UTF-8 or latin-1 (they used to be stored garbled, and names in the OSPF map as hex). Text stored
  wrongly is corrected at the next re-scan or OSPF build. A device without the interface-name table
  (ifXTable) gets its interfaces from ifDescr instead of none.
- **Warning about æøå in SNMP settings:** a community, v3 user or password with characters outside ASCII
  still works, but gives a warning when it is entered (discover, host edit and the default community).
- **Tests that bad input is stopped:** new tests check that a request without or with a wrong CSRF token
  is refused and changes nothing, that the login only redirects to paths inside the app, and that hostile
  text in host and map names is shown escaped. Three unused pieces of code are removed.
- **Contributing, security policy and code of conduct:** `CONTRIBUTING.md` (how to report and send
  changes to a mirrored repository, and that contributed code can be distributed under both licenses),
  `SECURITY.md` (how to report a vulnerability and what a report must contain) and `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1).
- **Third-party licenses:** `THIRD_PARTY_LICENSES.md` lists everything Bifrost builds on — the files under
  `webapp/static/vendor/`, the Python packages, the container images and the map tile services — with the
  license each is under. The vendored files have their license text next to them, and a test fails when a
  package, a vendored file or an image is missing from the list. The default router icon is Bifrost's own
  and moved out of `vendor/`.

## 2.3.9 — About page links (2026-09-29)

- **Repository links:** the About page, the README and `docs/Home.md` point to the previous repository
  address again.

## 2.3.8 — English user interface (2026-09-29)

- **The whole user interface in English or Norwegian:** menus, pages, buttons, messages, graphs, maps and
  the map editors, the admin pages and the info screens. «Språk / Language» under Admin → Settings sets the
  installation's language (Norwegian by default), and each user can choose their own on My page (Min
  side) or follow the installation. Dates, numbers and singular/plural forms follow the language, and so do
  the labels inside Google map tiles. The log keeps its entries as they were written.
- **Language packs:** each language is one file in `webapp/lang/`; a new file there appears as a choice by
  itself.
- **Fixes:** `.env.example` is described correctly in the README (its comments are in English), and an
  internal case reference is removed from a settings help text.

## 2.3.7 — Configuration example in English, time zone documented (2026-09-28)

- **`.env.example` in English:** every comment is translated; the settings and their default values are
  unchanged. The re-scan window (`RESCAN_HOURS`) is now described correctly: it follows the app's time
  zone.
- **Time zone documented:** the README explains that the zone chosen in Settings applies, that `TZ` in
  `.env` has no effect because `docker-compose.yml` fixes the containers' zone, and that the container
  logs are stamped in Europe/Oslo.
- **Data quality page:** the separator lines between the sections are drawn.

## 2.3.6 — Contact details and links (2026-09-28)

- **Contact details updated:** the author's name and e-mail address are updated in the page footer, on
  the About page, in the licence text in the README and `docs/Home.md`, and in every source file header.
- **About page links** point to the current source repository and wiki; the wiki link opens the wiki's
  front page instead of a page that does not exist.
- **Default theme:** the theme setting says just «Standardtema» when no custom theme is active, and the
  stylesheet comments describe the default look in neutral terms.

## 2.3.5 — Backup without a passphrase, theme upload fix, English documentation

### Fixes

- **An uploaded theme keeps all its colours:** a comment in the theme CSS made the colour after it
  disappear without a word — uploading the example theme kept only 19 of its 50 colours. Comments are
  now ignored correctly, and the save message lists any colour that was rejected (invalid name or unsafe
  value) instead of only a count.

### Backup

- **Unencrypted backup without a passphrase:** press Enter at the passphrase prompt and confirm with
  `j` to take an unencrypted backup (hot and cold). Restore recognises an unencrypted backup and does not
  ask for a passphrase; restoring an encrypted backup with an empty passphrase stops with a clear message.
  In cron, a backup without a passphrase still fails unless `BACKUP_PLAINTEXT=1` is set.

### Documentation

- **README and the documentation in `docs/` rewritten in English**, shorter and checked against the
  code. Corrected along the way: first login, map lifecycle (park and archive), access to logical maps
  per map, data retention, time zone, moving `.env` from an encrypted backup, and the settings, pages
  and commands that were missing.
- **nginx example:** upload limit and timeouts that match the app (larger uploads and slow map
  snapshots no longer fail with 413/504), notes on `TRUSTED_PROXIES`, `PROXY_COUNT` and keeping the app
  port off the network, and optional HSTS.
- **Theme example:** English comments, uploads without losing tokens, and the missing `--warning`,
  `--flow` and `--lsp` tokens added.

## 2.3.4 — Follow-up on external review (2026-09-27)

An external review of 2026-09-27 was verified against the code and handled point by point. It found no
security issues; most observations were already deliberate design choices, and a few function and field
names in the report were wrong. Two small clean-ups came out of it.

### Clean-up

- **Adding a host to a map uses the placement engine directly:** the old compatibility layer left over
  from the placement-engine merge is removed. Where a newly added host lands, and the check for a full map
  grid, are unchanged.
- **Corrected code comment on the CSRF exemptions** (comment only; the protection is unchanged).

## 2.3.3 — Fixes and clean-up after internal review (2026-09-24)

Findings from the internal review of 2026-09-24 and the review of 2026-08-24, verified against the
code and handled one by one (both reviews are now archived in the definition repository). Remaining
items are tracked in the issue register.

### Fixes

- **"Restore version" overwrites what you are working on, not the saved draft:** restoring used to
  delete a saved draft while an ongoing working copy was left behind (and opened by "Edit"). The
  version now becomes the working copy; the active map and the saved draft are not touched. Nodes are
  kept inside the map frame.
- **"Save draft" overwrites the previous draft:** a map could end up with two saved drafts, and "Edit"
  opened an arbitrary one. There is now at most one draft per map (physical and logical).
- **A map link switched off stays off:** a link you switched off in the editor could come back as "on"
  if its interface temporarily disappeared (paused host, changed role etc.). The choice is now kept.
- **The map editor shows when an action fails** (delete node/link, replace link, swap device) instead of
  pretending it worked.
- **Background-map snapshots have a total time limit (90 s):** a slow tile server used to end in a
  broken response. It now stops with an explanation and keeps the existing background.
- **Maps stay inside their frame:** editor coordinates that were not valid numbers caused a 500 (save and
  auto-arrange), and auto-arrange could return positions outside the map. Everything is now clamped to
  the map frame; on logical maps the node is always saved (keeping its position on an invalid value). A
  node right at the edge (x/y = 0) also starts where it is when running "Improve".
- **Traffic flow: dominant role** could be won by a role with no valid traffic — giving the wrong
  label/colour. The node ordering in the API response had the same flaw (a node missing one direction
  sorted first) and is fixed too.
- **The map view does not write before the access check:** opening a draft through an old link without
  access returned 404, but refreshed the draft's links first.
- **Errors from buttons are shown:** when an action failed on the server (e.g. no access, conflict,
  internal error) it looked as if the button did nothing. A dismissable message now says what went wrong.
- **Validation:** a host cannot be saved with an empty name; classification rules reject an invalid
  role; the Data quality buttons return 400 (not 500) on an invalid timestamp. Settings are validated by
  type — numbers must be integers within limits, the NetBox URL must be `http(s)://` with a valid IP
  address or host name, the ASN must be an AS number — the same on Settings and Data quality (the latter
  used to adjust the value silently).
- **"Back" on the No access page works** (it was blocked by the security policy).
- **The GPS panel shows the position lock on both map types:** on physical maps it showed "Manually
  locked" from an unbuilt GPS lock (always "no"), so a node locked with "Lock position" read "no". It now
  shows "Locked position" from the x/y lock, the same as on logical maps.
- **A custom theme with quotes** (e.g. font names) was broken in the output — now rendered correctly.
- **The physical map editor shows feedback from auto-arrange** (e.g. "The layout is already good",
  errors) — the messages used to appear on logical maps only.
- **Maps and NetBox search show the server's error message** instead of a generic error (or nothing)
  when a call fails or the session has expired.
- **API errors as JSON:** some API/passkey calls answered with an HTML error page instead of JSON.
- **The customer graph showed more than the group has access to:** called directly without a world,
  `/api/customer` returned both the Internet and the transport series even if the group only had access
  to one. You now get only the series you have access to.
- **The Data quality page with too much data:** a search that exceeded the database's time limit
  (typically many days) showed "Database unavailable". It now explains that the data volume was too
  large (try fewer days), and the event is logged.
- **The SNMP community is always stored on the host:** leaving the community field empty in "Discover
  host" scanned the device with "public" (not the default from Settings) and stored the host without a
  community. The default from Settings is now used and stored on the host — also when editing. SNMPv3 is
  unchanged. Older v1/v2c hosts without a community get the system default stored once at startup.
- **Logical maps were deleted after park/restore:** "Park" and "Restore" on custom logical maps did not
  set `updated_at`, so the draft clean-up (28 days) could delete a map last changed long ago within the
  hour. It is now set, as for physical maps.
- **Backup clean-up had failed since 2.3.0:** a file pattern with no match (e.g. no unencrypted files)
  stopped `backup-hot.sh`/`backup-cold.sh` before old backups were removed, and cron got an error code
  every night. The backup itself was fine all along.
- **NetBox sync cleared site GPS on errors:** if fetching site details failed (timeout/5xx), the site's
  position was set to empty. GPS is now only overwritten with data actually read from NetBox.
- **Hosts without OSPF were merged into one node:** if a host answered "No Such Object" for the router
  id, the error text was used as the id. The host now gets its own IP as router id and becomes its own
  node.
- **Password changes invalidate all sessions:** the CLI commands `set-admin-password` and
  `reset-user-password` now log out all existing sessions (as the web does) and use the same password
  rule as the web (at least 12 characters). Changing your own password logs out your other devices.
- **Passkey challenges expire after 5 minutes:** the one-time code for passkey login/registration had no
  expiry. It is now rejected after 5 minutes, and login and registration each have their own slot in the
  session. If a passkey attempt fails (also on expiry), the next attempt fetches a fresh code — before,
  the page had to be reloaded.
- **The info screen only sends what it shows:** node data for the anonymous info screen also contained
  management IP, site, GPS and host id, although the screen only draws the map. Only what the view needs
  is sent now. (`/public/map` requires login and is unchanged.)
- **Max nodes per map is enforced when nodes are added:** the limit (Settings, default 100) was only
  checked on save, so auto maps (all hosts), "add host/router", ghost neighbours and automatically built
  area maps could grow past it — and large maps made placement very slow. Additions over the limit are
  now rejected with a clear message; automatically built area maps are filled up to the limit and the
  rest is logged.
- **Graphs showed a false 0 and a too-low total:** a time bucket without valid readings was shown as 0
  instead of a gap, and "total" lost an interface entirely when only one direction was invalid (e.g. a
  counter reset). Applies to the Internet, Backbone, counterparty and customer graphs. Missing data is now
  a gap, and the total counts the valid direction.
- **Restore is safer:** the backup file is checked in full before anything is deleted (a truncated or
  corrupt file — including an encrypted file with damaged bytes — is rejected and the existing data is
  left untouched). A cold restore extracts next to the existing data and swaps it in only after success.
  If `pg_restore` fails half-way, the database is still taken out of restore mode, Bifrost is started and
  the error is reported.
- **The poller matches counters to interfaces by OID:** answers from the device used to be matched to
  interfaces by their position in the response. A device answering with fewer values than requested
  could therefore put counters on the wrong interface without anyone noticing. Each answer is now matched
  on its OID.
- **One missing interface no longer takes an SNMPv1 host down:** v1 rejects the whole request if one OID
  is missing, and the host was marked down. That OID is now left out and the rest is fetched; the missing
  one is logged (check the inventory).
- **The poller limits concurrent hosts** (`POLL_CONCURRENCY`, default 64) — same behaviour as before
  below the limit, but protection against fd/ulimit trouble with many hosts.
- **"Re-scan" on a single host goes through the SNMP guard:** like every other admin and background path,
  a device is no longer queried more often than the cooldown (at least 60 s), whoever clicks. When
  rejected, the reason and the seconds remaining are shown. (The poller is deliberately exempt.)
- **Your own classification rules take precedence over the built-in ones:** a rule you added now wins
  over the built-in guess when role/counterparty/ASN are suggested; the built-in rules only fill gaps.
- OSPFv3 (untested): the area is chosen deterministically, as for v2.
- **The port name is the key, ifIndex follows:** if a port gets a new ifIndex (e.g. after a reboot), the
  re-scan recognises it by port name and re-points the interface automatically — same interface, so
  history, role, maps and peaks continue. Every re-pointing is logged. If the new number is already used by
  another interface, nothing changes (logged).
- **Re-scan as soon as a device has restarted:** the poller detects a reboot in the same request as the
  counters (snmpEngineBoots increases; otherwise snmpEngineTime or sysUpTime going backwards — the
  sysUpTime wrap after ~497 days does not count as a reboot). A re-scan of that host is then queued at
  once, so new ifIndex values are followed within minutes, not at the next daily re-scan. It goes through
  the SNMP guard as usual. A single-host re-scan no longer postpones the daily re-scan of all hosts.
- OSPF v2: one unexpected neighbour-state value no longer drops the whole router from the map build.
- **Access is granted on the map, not on an OSPF area:** the group form offered "Auto-area" rows that
  were never saved, and Edit on area maps checked an area object instead of the map. The area rows are
  gone; area maps are listed in the map list and access is set there, as for every other map.
- **The login lockout is no longer reset by a successful login:** before, one could try a few passwords
  against another user, log in with one's own account and get new attempts. The lockout now expires by
  time.
- **The session cookie gets the Secure flag automatically over HTTPS:** the flag now follows the actual
  connection (via nginx's X-Forwarded-Proto) — set over HTTPS, not over plain HTTP, so nobody is locked
  out and HTTPS is protected without configuration. `COOKIE_SECURE=1` still forces it on.
- **Logical maps have the same access control as physical ones:** access is granted per map (group level)
  and applies to the whole chain (draft, parked, working copies) — however many areas the map covers,
  including the automatic area maps. Drafts/parked maps are only shown to those with write access, and "no
  access" on a map always applies. The general "Logical map (OSPF)" grant is gone; groups that had it got
  read access to all logical maps that existed at the time of the transition.
- **"Discover host" in several tabs no longer mixes settings:** each discovery gets its own key that
  travels with the form in its tab, and "Save" only uses the settings (SNMP and NetBox placement) that
  belong to it. Before, a host could be saved with the settings from a discovery in another tab.
- **The last point no longer dips on 7d/30d and 12–48 months:** the hour/day in progress is left out
  until it is complete (the same rule as the 36h graph). The last point is the previous full hour, or
  yesterday.
- **"Maintenance" no longer loses peaks on errors:** deleting and recomputing peaks happens in one
  transaction — if the recomputation fails, the old peaks remain. The window is kept within the 90 days of
  raw data.
- **The aggregates repair their own history:** at startup it is checked whether an aggregate is missing
  history that raw data still has (e.g. after an interrupted backfill or a time-zone change), and if so it
  is filled.

### Clean-up

- **Startup does not hang requests:** while database setup runs in another process, a request waits at
  most 2 s (before: up to 180 s, more than gunicorn allows). Setup only counts as done when every part has
  succeeded, so a failed part is retried — with a growing interval (30 s → 30 min), so a persistent error
  does not cause table locks and the same warning every 30 seconds.
- **Demo loading is all-or-nothing:** if anything fails along the way, nothing is added.
- **Pillow 12.3.0:** upgraded from 10.4.0, and image uploads/map tiles now only try the PNG, JPEG and WEBP
  decoders.
- **Test for the anonymous info screen** (access boundary, and that the map slide only sends display
  fields), also in CI.
- **CI check of external map sources:** fetches one tile per source and fails if a source does not answer
  or has changed its address/API (Google only when a key is set).
- **The category page uses the same classification table as admin** (read-only) — same title, columns and
  role names, one source.
- **"Purge peaks" archives with explicit columns**, so a future column in the peaks table does not shift
  the values in the archive.
- **New setting "Max map tiles per snapshot"** (default 400) — the limit for how large an area a
  background-map snapshot can fetch, previously hard-coded.
- **Less duplicated code:** live link data for the logged-in map, the public map and the info screen is
  built by one shared function; NetBox ghost sync uses the same site lookup (with name validation) as
  host sync; the SNMPv3 protocols in the forms come from the same list as the validation.
- **All dependencies pinned:** `requirements.txt` for webapp and poller pin exact versions, including
  packages pulled in indirectly, and the Python base image is pinned to `3.12.14-slim-trixie`. The same
  code therefore gives the same build; upgrades are made deliberately with a new version.
- **Old background jobs are cleaned up:** finished jobs older than 30 days are deleted daily (the newest of
  each kind is kept), and the job table got an index.
- **The `./brand` mount is removed:** logo and colour theme have lived in the database (Admin → Settings)
  since 1.3.5, and the app no longer read anything from `./brand` — but its content was still served at
  `/static/brand/`. The mount and `brand/README.md` are removed; the example theme in `docs/` is updated.
- **Backup/restore:** the helper image `alpine` is pinned to `alpine:3.24.2` (used for tar/find on the DB
  volume), so a restore does not suddenly run against a new, unknown version.
- **README rewritten in English.** From this version on, the changelog is written in English.

Code confirmed unused (grep across code, templates, JS, tests, docs, CI and dynamic URLs) has been
removed. No functional change.

- **OSPF:** the old area-editor routes `/logical/<area>/save-layout|relayout|save-draft|discard-draft`
  and `_apply_save_draft` are removed — the editor only goes through the `map_*` routes (map_id). The area
  variant of `save-layout` also wrote straight into the published map, bypassing the working-copy model.
- **Physical map:** the old GPS placement (`/maps/<id>/place-gps`, `place_by_gps`, `_cluster_same_site`)
  is removed — GPS placement happens through auto-layout with GPS relative/exact.
- **Placement:** `layout.py` is slimmed down to the two entry points in use (`layout()`,
  `grid_capacity()`); unused `Params`/`DEFAULTS`/`quality()` and `placement._ccost` are removed.
- **Inventory:** the unused route for single-host refresh (`/admin/host/<id>/refresh`) is removed.
- **Miscellaneous:** a dead `grants` parameter in `create_user`, an unused import/variable in infoscreen,
  an unused logo and `.wordmark` CSS, company-specific comments in CSS reworded, and a dead port check
  plus an unused variable in the integration CI.

## 2.3.2 — CI flyttet til ny git-server (G-375)

Repoet er flyttet til en ny git-server. Ingen funksjonelle endringer i appen.

- **CI kloner fra instansens egen URL:** begge workflows hadde git-verten hardkodet i clone-URL-en og
  feilet etter flyttingen. Verten hentes nå fra `GITHUB_SERVER_URL` (satt av Gitea Actions), så CI
  følger repoet dit det ligger — også ved en senere publisering. TLS verifiseres fortsatt.
- **Én runner:** kjøres nå på en organisasjons-runner. To runnere på samme host kunne kjøre
  integrasjonstestene parallelt og kollidere på samme vertsport.
- README peker på ny git-adresse (kom rett etter taggen — følger neste release).

NB: `v2.3.2` ble tagget før versjonsstrengen ble bumpet, så appen i selve taggen rapporterer
**2.3.1** (footer/About/healthz). Bevisst latt stå i stedet for å flytte en pushet tag — releasen
er ren CI-konfig uten appendring. `review` rapporterer 2.3.2 fra og med neste commit.


## 2.3.1 — Lisens: AGPL-3.0-or-later + kommersiell (G-373) — realisering av planlagt endring

**Dette er ikke en ny beslutning, men gjennomføringen av en endring som ble planlagt 2026-06-12.**
Den gang (G-155) ble opphavsretts- og lisens-føringene lagt: hva opphavsretten dekker (design,
datamodell, måten trafikken sammenstilles og presenteres på — særlig Fordeling-siden og den
automatiske genereringen/plasseringen av nettverkskartene), og at alt dette skulle legges under en
**dual-lisens**. Selve lisensvalget ble **bevisst parkert 2026-06-15** i påvente av Rogers
beslutning, og About-siden har siden stått med «en dual-lisens er det mest sannsynlige utfallet …
flere detaljer kommer». **2026-08-09 er valget tatt**, og teksten realiseres nå:

- **Åpen side: `AGPL-3.0-or-later`.** Valgt fordi AGPL — i motsetning til GPL — også dekker det å
  tilby en *endret* Bifrost videre som nett-tjeneste. Det verner nettopp det opphavsretts-avsnittet
  peker på (kart-/sammenstillings-algoritmen) uten å stå i veien for at andre kan bruke og endre
  appen for eget nett.
- **Kommersiell side: på forespørsel.** For de som ikke kan leve med AGPL-forpliktelsene. Bevisst
  holdt som en åpen mulighet uten forhåndsdefinerte vilkår — ikke en forretningsmodell.
- **`LICENSE`** lagt til i repo-rota (full AGPL-3.0-tekst).
- **Alle 126 egne kildefiler stemplet** med `SPDX-License-Identifier: AGPL-3.0-or-later` i headeren
  (engangsjobb). Fire filer som manglet Bifrost-headeren helt fikk den samtidig. Tredjeparts-filer
  under `webapp/static/vendor/` (htmx, Leaflet, uPlot) er bevisst **ikke** stemplet — de beholder
  sine egne lisenser.
- **About-siden** og **README** oppdatert med det faktiske valget (erstatter «flere detaljer kommer»),
  inkludert oversikt over tredjeparts-lisenser.

Ingen funksjonelle endringer i denne versjonen — kun lisens-merking og dokumentasjon.


## 2.3.0 — Review-fikse-batch: to uavhengige AI-reviews verifisert og adressert (G-349–G-372)

To eksterne modell-reviews (2026-08-09) ble kryss-verifisert mot koden punkt for punkt: 34 unike
funn → 19 reelle fikset her, 6 behandlet etter beslutning, 9 avkreftet som misforståelser.
Ingen skjema-endringer; alt er additivt/atferds-fikser.

**Rettet — samtidighet/bakgrunnsjobber:**
- **Advisory-lås-kollisjoner (G-349):** tre par av uavhengige jobber delte låsenøkkel og blokkerte
  hverandre stille (OSPF-discovery ↔ daglig Topper-fold; LSP-bygg ↔ NetBox-sync; metrics-boot ↔
  jobb-kø). Alle nøkler bor nå i ETT register i `db.py`; tre nøkler omnummerert.
- **NetBox-sync ombygd (G-353):** holdt én transaksjon (+ rad-låser på host) åpen gjennom alle
  HTTP-kall (opptil 90 s × 2) — kunne blokkere polleren og host-redigering. Nå: session-lås,
  HTTP uten åpen transaksjon, kort skrive-transaksjon per host. Cooldown brennes ikke lenger av
  taperen ved dobbeltklikk. Felles heartbeat-hjelper for ALLE jobbtyper (netbox-sync manglet).

**Rettet — visning/data:**
- **Infoscreen viste falsk «NEDE» (G-351):** stale/upollet link på kiosk-skjermen ble rød «NEDE» i
  stedet for «ingen data» — G-272-regelen var aldri tatt inn i infoscreen-ruta.
- **Skjulte hoster hang igjen i tallene (G-354):** en myk-slettet (skjult) host ble stående i
  grafer/Fordeling/Topper/kundelista for alltid; skjulte interface ble re-skapt som spøkelses-
  lenker på kart ved hver åpning. (Pause er BEVISST uendret i historikk-vinduene.)
- **Fordeling «Nå» etter pause (G-363):** pauset host forsvinner nå straks fra nå-visningen
  (før: hang igjen med siste rate i opptil ~30 min).

**Rettet — sikkerhet/robusthet:**
- **Kart-lenke scope-lekkasje (G-350):** `create_link` sjekket ikke at interfacet tilhørte lenkens
  noder — en write-bruker kunne feste et fremmed interface og lese dens live-trafikk/historikk.
- **Passkey-sletting uten bekreftelse (G-352):** CSP blokkerte inline `confirm()` → «Fjern»
  slettet umiddelbart. Også fire CSP-drepte form-resets (hx-on) erstattet med delegerte mønstre.
- **`webauthn==3.0.0` + `tzdata==2026.3` pinnet (G-356)** — verifisert mot full passkey-citest.
- **Tomt kart ga 500 (G-355):** «Prøv på nytt» på kart uten noder krasjet motoren; også tak på
  links i lagring (batch-UPDATE) og lengdetak på node-etiketter.
- **OSPF-/LSP-samlerne validerer IP ved bruk (G-357)** — samme vern som polleren.
- **Backup-rettigheter (G-360):** `umask 077` + `chmod 700` — dumper/.env ikke lenger
  verden-lesbare. **Kart-flis-format-allowlist (G-361)** i geo-stitching.

**Herding (G-369):**
- **Passord-kryptert backup + restore:** backup-artefaktene (`*.dump`/`*.tgz` + `.env`-kopien)
  krypteres med `openssl` AES-256/PBKDF2 (600k iter) og lagres som `*.enc`; samme passord kreves
  ved restore. Passord via `BACKUP_PASSPHRASE` eller skjult prompt — aldri lagret på disk ved siden
  av dataen. Fail-closed for cron (ingen stille klartekst); `BACKUP_PLAINTEXT=1` for bevisst
  ukryptert. Restore verifiserer passordet før den destruktive operasjonen (ingen halv-restore) og
  leser fortsatt gamle ukrypterte backuper.
- **Sesjons-id hashes (SHA-256) i `app_session`:** klienten bærer den rå id-en i cookien, DB lagrer
  kun hashen. En stjålet DB-dump kan ikke lenger replayes som innlogging. NB: eksisterende økter
  blir ugyldige én gang ved oppgradering (alle må logge inn på nytt — ikke-destruktivt).
- **`.env`-herding dokumentert** (`chmod 600`); backup-katalogen lages `chmod 700` (fra G-360).
- **Vert-herding dokumentert (G-372):** nytt README-avsnitt «Herding av verten» — kryptert disk
  (LUKS) under prod-volumet dekker disk/server-tyveri (som app-krypto bevisst ikke gjør). Bygger på
  premisset at Bifrost ikke holder reelt sensitiv data;
  sakene G-015/G-367/G-370/G-371 er lukket på samme grunnlag.

**Rettet — drift/vedlikehold:**
- **about() logger nå feil (G-358)** etter G-325-konvensjonen i stedet for stille `pass`.
- **Branding TTL-cachet + nav-spørringer hoppes over for anonyme (G-359)** — sparer ~4 DB-rundturer
  på hver login-visning og 2 på alle andre.
- **API-feilkontrakt (G-364):** `{error: stabil kode, msg: norsk tekst}` — dokumentert og
  avvikerne konvertert; frontend viser `msg || error`.
- **Ny testdekning (G-365 + G-368):** rate-lagets hjelpere, settings-presedens, branding-filteret,
  Argon2id-stien, placement-guards, NetBox-parsing (104 enhetstester i Tier A+B, før 45) + tre nye
  integrasjonstester i Tier C: draft→live-merge-semantikken (14 sjekker), jobb-kø-samtidigheten (9)
  og rate-SQL-en mot ekte DB med kjente tellere (6). **Tier C kjører nå på ALLE pusher til review**
  (før: kun ved boot-kritiske filendringer).
- **CI verifiserer TLS mot git-serveren (G-366):** `sslVerify=false` på token-bærende clone-URL
  fjernet — git-serveren har gyldig offentlig sertifikat, flagget var arv fra CI-oppsettet.
- Død `MOCK_SNMP`-konfig fjernet (G-362).

## 2.2.11 — Site-normalisering fullført: dropp legacy host.site/lat/lon (G-347, review)

Contract-fasen (avslutning) av site-normaliseringen fra 2.2.8 (G-286). Ingen ny funksjonalitet — site
+ GPS leses og skrives nå KUN via `host → site`; de gamle per-host-kolonnene er borte.

- **Slutt på dual-write:** NetBox-sync, discover-lagring og demo-seeding skriver nå kun `host.site_id`
  (+ site-tabellen), ikke lenger `host.site/lat/lon`.
- **Fjernet COALESCE-fallbackene** (`COALESCE(s.x, h.x)`) i hostliste, alle kart-/plasserings-spørringer
  og OSPF → leser kun `host → site`. OSPF beholder ghost-fallbacken (`COALESCE(s.name, n.site)`).
  Konsekvens: sletter du en importert site, mister hosten site-visningen umiddelbart (ikke lenger det
  gamle navnet fra en etterlatt kolonne).
- **Droppet kolonnene** `host.site`, `host.lat`, `host.lon`. Migreringen er robust for et **direkte
  versjonshopp** (prod hopper rett til 2.2.11 uten å innom 2.2.8–2.2.10): den populerer site-tabellen
  FRA legacy-kolonnene og dropper dem så i ETT boot-pass, i riktig rekkefølge. Idempotent (kjøres kun
  mens `host.site` finnes → no-op ved re-boot etter drop) og **ikke-destruktiv**: en verify-guard
  dropper kun når ingen aktiv host har site/GPS som ikke er fanget via `site_id` — ellers beholdes
  kolonnene og det logges (undersøk + re-boot). `host.netbox_id` og `composer.map_node.lat/lon` er urørt.

## 2.2.10 — Logisk kart: ekte interface-match på OSPF-naboer (G-344, review)

- **Riktig lokalt interface (→ område + metric) for hvert OSPF-naboskap.** Naboskapet selv har
  alltid vært ekte (fra `ospfNbrTable`), men *hvilket lokalt interface* det hørte til ble gjettet med
  en grov `/24`-heuristikk (første 3 oktetter + første treff). På fler-område-nett der to P2P-linker
  delte samme `/24` kunne den plukke feil interface → feil område/metric.
- Henter nå **ekte nettmaske** via SNMP (`ipAdEntNetMask`) i tillegg til ipAddrTable-oppslaget vi
  allerede gjorde, bygger det faktiske lokale prefikset pr. IP, og matcher naboens IP inn med
  **longest-prefix-match**. Match-stige: (1) ekte prefiks; (2) `/24`-fallback når maska mangler for
  interfacet; (3) `single_area`/ukjent metric når ingen lokal match (f.eks. unnummerert interface) —
  naboskapet vises uansett, det droppes aldri.
- Kun IPv4/OSPFv2 (der problemet er). OSPFv3/IPv6 er urørt (hører til G-343). Endringen er
  selvinneholdt i `ospf.py` — ingen poller-, inventory- eller skjema-endring.

## 2.2.9 — «Flipp / flopp kart» i editoren (G-292, review)

- **Speilvend hele plasseringen med ett klikk.** To knapper i den felles redigerings-raden:
  **⇋ flipp** (øst/vest, speil venstre↔høyre) og **⇅ flopp** (nord/sør, speil opp↔ned). Nyttig når
  auto-plasseringen er god, men landet som speilbilde av geografisk intuisjon — slipper å dra alle
  nodene manuelt.
- Speiler **alle** noder om deres egen bounding-box-senterlinje (klyngen blir stående, bare vendt).
  Treffer alt, også **låste/pinnede** noder — hele bildet snus, den speilvendte plasseringen blir den
  nye. Geo-bakgrunn påvirker ikke (geo er hint, ikke fasit).
- Ren arbeidskopi-operasjon: ikke persistert før «Lagre utkast»/«Aktiver» (som auto-layout). Ufarlig —
  klikk samme knapp igjen = eksakt angre. Felles kjerne (`map_renderer.js`) → virker på **begge**
  karttyper (fysisk + logisk) via den delte editoren.

## 2.2.8 — NetBox site-import + normalisert site-tabell (G-286, review)

Normaliserer site + GPS til egen tabell og lar operatøren importere enheter fra NetBox via det
eksisterende «Oppdag via SNMP»-feltet. Bygger grunnlaget for senere site/location-gruppering (G-347).

- **Ny `site`-tabell** (`netbox_id` unik, `slug`, `name`, `lat`/`lon` nullable) + `host.site_id`
  (ON DELETE SET NULL). GPS bor på siten, ikke på hver enhet — flere enheter deler én site.
  Migrering (idempotent): eksisterende `host.site/lat/lon` → site-rader + link. Reconciliation-upsert
  matcher `netbox_id` → `name` → insert (fyller migrerte navn-rader med id+slug ved neste sync). De
  gamle `host.site/lat/lon` beholdes (dual-write) én overgang — droppes i G-347 (contract).
- **NetBox-import i «Oppdag via SNMP» (to operasjoner):** (1) skriv hostnavn (min. 3 tegn) → live
  NetBox-søk (autocomplete) viser treff (navn + site); (2) velg enhet → enhetens IP hentes fra NetBox
  sitt detalj-oppslag (primær-IP hvis satt, ellers første aktive IPv4 fra IPAM `?device_id=`) og fylles
  i feltet → dagens discover-flyt kjører. (Primær-IP hentes bevisst i detalj-oppslaget, ikke fra
  list-søket.) Skriver du IP direkte, gjøres et eksakt NetBox-oppslag ved discover. **NetBox er fasit**
  for hostnavn + site over SNMP (avvik meldes til operatøren). Enheten kobles til sin site allerede ved
  lagring. Ekstern NetBox-tekst valideres (slug-allowlist, navn strippet for kontrolltegn, lengde-cap);
  vises escapet + kappet til 30 tegn.
- **Bulk-sync** («Hent posisjoner fra NetBox») populerer nå `site`-tabellen + linker host, i tillegg
  til dual-write. Per-site **«oppdater»** re-henter én enkelt site uten å synke alt.
- **Ny admin-side «Importerte sites»** (lenke fra NetBox-kortet): read-only oversikt (slug/navn/GPS/
  netbox-id/antall enheter, utvid til enhetsliste), med **slett** (host beholdes, mister site-kobling)
  og **oppdater** per site.
- **Kilde-bytte:** kart (fysisk + logisk), node-info og hostliste leser nå GPS/site via `host → site`
  (COALESCE-fallback til gamle host-kolonner i overgangen). Ghost/OSPF-noder uendret. Site/GPS tvinges
  aldri — Bifrost fungerer fint uten NetBox/site/GPS.
- **Node-info i normal kart-visning:** operatør + admin ser nå en `📍 <slug> (lat, lon)`-linje i
  node-popupen (host→site — ende-til-ende-bevis); host-lenken går til Admin (admin) eller Inventar
  (operatør). Editor-panelet er uendret. Vanlige brukere/public ser ikke site/GPS.
- Verifisert i test-bifrost: 45 unit + 78 Tier C grønt; migrering + reconciliation, host→site-oppslag,
  admin-side + slett (SET NULL, host beholdt), graceful uten NetBox.

## 2.2.7 — opprydding: robust migrering synlig i Logg + fjernet legacy-constraint (G-345, review)

Bygger på 2.2.6-savepointet: gjør robustheten synlig og fjerner kilden til utestengings-buggen.

- **Hoppede migreringssteg rapporteres til Logg-sida.** `_try_migrate` samler hvert steg som ble
  hoppet over (savepoint-rullback pga. gammel data) i en liste; `report_skipped_migrations()` spiller
  den av som WARNING til `log_event` ETTER at DB-logg-handleren er koblet på (app.py). Boot-interne
  log-linjer nådde ellers aldri log_event (handler ikke på + log_event låst av migrerings-transaksjonen)
  — nå ser en operatør uten shell nøyaktig hvilke steg som ble hoppet, i GUI-et. Erstatter den
  midlertidige 2.2.6-debug-linja.
- **Fjernet `user_grant_kind_check`.** `user_grant` er en legacy-tabell (2.0.0 flyttet tilgang til
  grupper/ACL; ingen levende kode skriver til den). CHECK-en vokter ingenting — men en gammel rad som
  brøt den var nettopp det som abortterte config-migreringen ved 1.6→2.x-oppgradering (→ `app_session`
  aldri opprettet → login 500 → utestengt; på 2.2.5 traff `rate_event` først). Vi DROPper den bare
  (konsistent på alle servere) og gjenoppretter den ikke → aborten kan ikke oppstå fra dette igjen.
- **Ryddet 500-diagnosen.** Den midlertidige «Feiltype:»-visningen på 500-siden (fra G-345-feilsøkingen)
  er fjernet nå som login-buggen er løst i felt. Uhåndterte exceptions logges fortsatt med full traceback
  til `log_event`/Logg-sida (den varige verdien) — brukeren ser en nøytral feilside uten detaljer.

## 2.2.6 — robust config-migrering (G-345-relatert, review)

- **Migreringen aborterer ikke lenger på gammel data.** `ensure_pg_config` kjørte alle
  additive migreringer i ÉN transaksjon: feilet én data-avhengig setning (en CHECK-constraint
  eller type-endring som eksisterende rader fra en gammel versjon bryter), abortterte HELE
  transaksjonen — og alt lenger nede (bl.a. `CREATE TABLE rate_event`) ble aldri kjørt. Resultat:
  på en oppgradert server manglet tabeller som nyere kode trenger → 500 (f.eks. login, som bruker
  rate-limit-tabellen `rate_event`). Nå kjøres hver data-avhengig setning i et **SAVEPOINT**
  (`_try_migrate`): feiler den, rulles kun det steget tilbake og **logges til Logg-sida** (log_event,
  GUI-synlig) — resten av migreringen fullfører. Beskyttede steg: `interface_role_check`,
  `user_grant_kind_check` (×2), `map_status_check`, `map_kind_check`, `composer.map`
  created_at/updated_at → timestamptz. Verifisert (test-bifrost): en constraint som brytes av
  eksisterende data hoppes over uten å hindre at tabeller lenger nede opprettes.
- Interne logger styrkes som primær feilsøkings-kilde for servere uten shell-/logg-tilgang:
  uhåndterte exceptions (full traceback) og migrerings-skip surfaces i `log_event`/Logg-sida.
- **DEBUG (midlertidig):** ved boot logges hvilke `user_grant.kind`-verdier som bryter regelen til
  Logg-sida (`warn_legacy_grant_kinds`, kjøres ETTER at DB-logg-handleren er koblet på — ellers når
  ikke boot-logger log_event) → identifiserer KILDEN til den avortert-migrering-buggen. Fjernes når
  kilden er håndtert.
- (Midlertidig 500-diagnose-visning beholdt inntil G-345 er bekreftet løst i felt; fjernes deretter.)

## 2.2.5 — rate-plausibilitet & datakvalitet-verktøy (G-346, review)

Permanent fiks for teller-glitcher og kapasitets-steg (LACP/AE-interface som endrer hastighet), basert
på ekte prod-funn. Samler og lukker den gjenåpnede G-306 sammen med G-305 (reset-guard) og G-342
(pålitelig fart) til én helhetlig modell.

- **Per-interface rate-tak (kjernen).** En rate regnes som umulig (teller-glitch/ifIndex-hopp/kapasitets-
  steg) og vises som **hull** (NULL), aldri spike, når den overstiger `interface-fart × faktor`. Erstatter
  det flate taket fra G-306 (som enten skjulte legitim trafikk på understated interface eller slapp
  gjennom moderate glitcher). Faktoren gir rikelig slark for legitim **over-subscription** (avtalt 25G på
  100G-link kan reelt kjøre langt over avtalt). Er farten ukjent/0 (loopback/irb før operatøren har satt
  den) brukes et **flatt backstop-tak** (konfigurert maks interface-hastighet). Innført på alle rate-
  produserende steder: rå 5-min + 1h/1d-cagg (hovedgraf, interface-historikk, trafikkart, peak-fold).
- **Manuell fart vinner alltid (`speed_locked`).** Overskrives aldri av innlest SNMP-fart — verken av
  periodisk re-skann (allerede G-342) eller ved discovery-lagring (ny guard). Da følger taket avtalt kapasitet.
- **To konfigurerbare tall** (Admin → Innstillinger *og* nederst på Datakvalitet-sida, samme app_setting):
  `Maks interface-hastighet / rate-tak` (default 10 Tbit/s — input-validering + flatt backstop + aggregat-
  basis) og `Rate-tak faktor (×)` (default **7** — per-interface tak + «Rens Topper»-aggregattak). Nye,
  flerfunksjons hjelpetekster + eget README-avsnitt som forklarer bruk og effekt.
- **Datakvalitet-sida bygget om:** forklaring av hva sida er (øverst), forklaring av hva Rett/Slett gjør
  (under lista), **smart diagnose + anbefaling** per funn (enkelt-spike→Rett, klynge/hull/kapasitetsendring
  →Slett; ★ markerer anbefalt), og et **feedback-vindu** (siste 5 vedlikeholds-jobber m/ status/resultat).
- **«La ligge» (kvitter ut):** marker en outlier som kjent/håndtert («ser feil ut, men er alt dekket av
  rate-taket / fikset») → skjules fra lista UTEN å røre rådata, reversibelt (angre nederst). Anbefales
  automatisk (★) når raten alt er over rate-taket — et teller-steg fra en kapasitetsendring som Slett
  uansett ikke kan fjerne rent (unngår «whack-a-mole»-sletting). Ny `dataquality_ack`-tabell.
- **Tungt vedlikehold kjøres asynkront** (worker-tråden, samme mønster som re-skann/bygg): heling etter
  reparasjon (cagg-refresh + Topper re-fold) og «Rens Topper» (full re-fold) gir ikke lenger 504 ved mange
  reparasjoner på rad — flere reparasjoner coalesces til én jobb over hele det berørte vinduet.
- Verifisert i isolert test (test-bifrost): 45 unit + 78 Tier C grønt; per-interface-tak beholder 3×
  over-subscription og skjuler 8× glitch; ukjent fart → flat backstop; manuell fart bevart ved re-lagring;
  async purge/heling kjører uten 504; sida rendrer med alle nye seksjoner.

## 2.2.4 — sikkerhet & datakvalitet: kodereview-batch (review)

- **G-296 (svake default-hemmeligheter):** Det innebygde admin-standardpassordet er ikke lenger
  literal `admin`, men `change-me-soon` (langt → generiske credential-stuffing-bots gjetter det ikke).
  Boot-advarsel + tvungen passordbytte ved første innlogging står som før (fyres nå for både
  `change-me-soon` og eldre `admin`). Denne endringen treffer kun *ferske* installasjoner — force-change
  gjør at ingen eksisterende install sitter på default → ingen migrering.
  - **DB-passord (`bifrost`-default) beholdt bevisst:** Postgres-porten publiseres ikke (kun docker-nett)
    → passordet er ikke nåbart utenfra. `.env.example` gir uansett `change-me-please`; `bifrost`-fallbacken
    treffer kun den som kjører helt uten `.env`. Å heve den ville brutt eksisterende default-volumer uten
    reell gevinst (dokumentert default er offentlig kjent). Prod setter `DB_PASSWORD` eksplisitt.
  - **SNMP-community `public` beholdt:** bevisst SNMP-standard, konfigurerbar, kun admin ser verdien (jf. G-297).
  - Merk: SECRET_KEY (session-signering) er allerede tilfeldig-generert per install (`secrets.token_hex(32)`)
    og var aldri svak. Nettosum: (b)+(c) bevist ufarlige av arkitektur, (a) lite gulv-løft på den ene åpne flaten.
- **G-305 (counter-reset/wrap → falsk 0):** En teller-reset (reboot) eller 32-bit-wrap ga negativ delta som
  `GREATEST(delta,0)` gjorde om til **rate = 0** — en falsk null som overlevde `in_bps IS NOT NULL` og ble
  midlet inn som ekte 0-trafikk (verst i 12–48mnd-grafene: en reset midt på dagen ga `in_last<in_first` →
  hele døgnet rendret 0). Nå: negativ delta / `last<first` → **NULL (hull, ukjent)**, ikke 0 — på alle 8
  rate-produserende steder (rå 5-min + cagg first/last, hovedgraf + interface-historikk + trafikkart + peak).
  Ekte flat 0-trafikk (`delta=0`) beholdes som 0. `find_outliers` uendret (skal bevisst bruke
  `GREATEST` for å finne hopp fremover). I tillegg forkaster ikke lenger et NULLet in-utfall den gyldige
  out-målingen: 6 filtre endret fra `in_bps IS NOT NULL` til `in_bps IS NOT NULL OR out_bps IS NOT NULL`.
  Verifisert (isolert Postgres): reset→hull, flat→0, én-retnings-NULL beholder motsatt retning. Kjenner
  restbegrensning: en intra-døgn-reset som klatrer tilbake over `in_first` samme døgn fanges ikke i 1d-stien
  (arkitektonisk grense ved first/last-per-døgn); 30d-stien (`_hist_day_tz` fra 1h-cagg) koster kun 1 time.
  (Dere kjører v2c+/64-bit HC → 32-bit-wrap er uansett kun teoretisk; behandles nå ærlig som hull.)
- **G-295 (SQL f-string-hardening / tz-validering):** Tidssone-innstillingen valideres nå mot den
  innebygde IANA-databasen (`ZoneInfo`) på både input (allerede: allow-liste + `ZoneInfo` ved lagring)
  og output (`db._configured_tz` + `queries._tz`) — sterkere enn den gamle tegn-regexen (sjekker at
  sonen faktisk FINNES, ikke bare at tegnene ser gyldige ut) og injeksjonssikker (et IANA-gyldig navn
  har kun trygge tegn; verdien bakes inn i cagg-DDL). Retter en villedende kommentar (`queries.py`
  påsto paritet med `_configured_tz` som ikke fantes) og fjerner en gjentatt cagg-ombygging ved boot
  dersom sonen ikke matchet. `_cagg_1d_ddl` beholder sin regex-guard som siste belt ved selve
  interpoleringen. Ubrukt `import re` fjernet fra `queries.py`. Øvrige f-string-SQL-steder (CTE-tabell/
  view-navn, ALTER-kolonne, poller speed-kolonne, ORDER BY) bygges alle fra lukkede, interne sett —
  mønster-risiko, ikke levende hull — og er dokumentert som akseptert (ingen `sql.Identifier`-omskriving).
- **G-301 (session-livssyklus, kanttilfeller):**
  - **F1:** `USER_MAX_DAYS=0`/`OPERATOR_MAX_DAYS=0` (dokumentert «0 = av») ga en server-sesjon med 0s
    levetid → umiddelbart utløpt → logout-loop. Nå betyr 0 «ingen absolutt maks levetid»: server-TTL
    settes til et langt utløp (≈10 år) konsistent med at app-side-sjekken alt er av. Økta dør da kun ved
    logout/disable/revoke. (Infoskjermen er uendret — den har egen token-basert URL-gyldighet, ikke
    `app_session`.)
  - **F3:** break-glass-admin-sesjoner (`user_id NULL`) kunne aldri revokeres — `destroy_for_user(None)`
    traff ingen rader (SQL `= NULL`). Ny `sessions.destroy_break_glass(keep_sid=…)` (`IS NULL`) kalles nå
    ved admin-passordbytte (`admin_password` + tvungen `force_change`) → UMIDDELBAR revokering av øvrige
    admin-økter etter kompromiss (spec §7), beholder den som nettopp byttet.
  - **F4:** passkey-innlogging stemplet `last_used_at` FØR eksisterer/enabled-sjekk → en deaktivert
    konto ble holdt kunstig «fersk» (ødela stale>180d-markeringen) og et avvist forsøk så ut som «sist
    brukt». `sign_count`/backup-status oppdateres fortsatt alltid (WebAuthn replay/klone-vern), men
    `last_used_at` stemples nå via `mark_used()` KUN ved fullført login (etter enabled-sjekk).
  - **F2:** utløpte `app_session`-rader ryddes nå ~1/døgn i den periodiske worker-jobben
    (`sessions.purge_expired`) — før vokste tabellen (kun opportunistisk >1 døgn ved `create`).
    `validate()` avviste alltid utløpte økter, så dette er ren opprydding, ikke et hull.
- **G-306 (rate-tak & destruktiv peak-purge):** Rate-plausibilitetsfilteret bruker nå et **flatt,
  admin-konfigurerbart tak** (`max_interface_speed_mbps`, default 10 Tbps) i stedet for per-interface
  `speed × 2` (som med en understated SNMP-fart — Juniper irb rapporterer ofte 1 Gbps men pusher mer —
  ga et latterlig lavt tak som skjulte LEGITIM trafikk). Kun astronomisk-umulige rater NULLes nå; de
  sjeldne moderate glitchene ryddes via Datakvalitet-verktøyet i stedet for å stille skjule ekte data.
  «Rens Topper» (`purge_impossible_peaks`) bruker et **aggregat-tak** = `max_interface_speed_mbps ×
  peak_aggregate_factor` (ny setting, 1–100, default 5 → 50 Tbps) — ikke lenger et flatt 2 Tbps som
  slettet legitime aggregat-rekorder (sum av mange interface kan legitimt >2 Tbps). Rader som slettes
  **arkiveres først til `peak_daily_purged`** (med `purged_at`) → reversibelt ([[no-destructive-code]]).
  `link_rates` og fold-stiene bruker samme flate tak. (`purge_metrics` urørt — den er scoped/tilsiktet
  sletting ved permanent host/interface-fjerning, ikke en bug. `find_outliers` beholder sin egen lavere
  terskel — den er datakvalitets-verktøyet, ikke et stille tak; videre utbygging = G-342.)
- **G-342 (interface fart-datakvalitet):** Rot-årsak-tiltak så rate-taket får pålitelig fart-data.
  (D3a-2) Re-skann lar ikke lenger en forbigående SNMP-`0` (`ifHighSpeed=0` ved agent-reboot/port-flapp)
  **overskrive en kjent fart** — `COALESCE(NULLIF(%s,0), speed_mbps)`; discover normaliserer også `0→None`.
  (D3c) Operatøren **må velge fart aktivt** ved add: manuell «+ interface» krever fart (≥1), og i
  discovery/re-skann får **ikke-fysiske** interface (loopback/irb/tunnel — der SNMP-farten er upålitelig)
  et editerbart, påkrevd fart-felt (fysiske beholder SNMP-farten). Alle fart-felt (add/discovery/edit) har
  nå en felles `<datalist>` med forhåndsvalg 1G/10G/100G, fritt redigerbar for egendefinert (f.eks. 1,6T).
  (D3b — flagging av «trafikk vs fart»-avvik i Datakvalitet = bevisst utsatt til egen fremtidig sak.)
- **G-310 (unik constraint på `if_sample`):** `if_sample` hadde kun en ikke-unik indeks på
  `(interface_id, time)`, og polleren inserterte uten `ON CONFLICT` → poller-retry / dobbeltstartet
  poller / delvis restore+repoll kunne legge inn **duplikat-sampler** → dobbelttalte deltaer/rater.
  Nå: en **UNIK indeks** `if_sample_iface_time_uq` på `(interface_id, time)` (inkluderer partisjons-
  kolonnen `time`, påkrevd på hypertable), og poller + demo-seed inserter med
  `ON CONFLICT (interface_id, time) DO NOTHING` (idempotent — retry dobbelttelller aldri). Ferske
  installasjoner får den unike indeksen direkte (`db/schema.sql`); eksisterende oppgraderes av en
  **engangs-migrering** (`_ensure_if_sample_unique`) som dedupliserer (behold én pr. nøkkel) og bygger
  den unike indeksen — guardet på at indeksen ikke finnes ennå, så den dyre dedup-scannen kjøres kun
  én gang, ikke ved hver boot. **Hotfix (komprimert hypertable):** `if_sample` er en KOMPRIMERT
  TimescaleDB-hypertable, og dedup-en kan ikke bruke `ctid` (finnes ikke på komprimerte chunks →
  «transparent decompression only supports tableoid»); uten dette feilet migreringen på eksisterende
  installasjoner → unik-indeksen ble aldri bygget → poller-ens `ON CONFLICT` traff ingen constraint →
  datainnsamling stoppet. Dedup er nå ctid-fri (temp-tabell m/ dup-grupper → slett gruppe → sett inn én
  representativ; `CREATE UNIQUE INDEX` er bekreftet lovlig på komprimert hypertable). Verifisert
  ende-til-ende mot en faktisk komprimert hypertable m/ injiserte duplikater. (Beslektet `ospf.lsp` og
  `rescan_job` vurdert: allerede beskyttet av hhv. transaksjonell delete+insert/serialisert bygg og
  advisory-lås → ingen endring.)
- **G-314 (fil-opplasting: MIME/CSS-herding):** (K1) logo- og kart-bakgrunns-opplasting håndhever nå en
  **format-allowlist** (`img.format` ∈ PNG/JPG/WEBP) etter `Image.open`, i stedet for å godta alt Pillow
  kan dekode (TIFF/GIF/ICO/…) — begrenser CVE-flaten. (K3) tema-CSS-denylisten blokkerer nå ALLE
  bilde-/ekstern-ressurs-funksjoner (`url()`, `image-set()`, `cross-fade()`, `element()`, `image()`),
  ikke bare `url()`. (K4) kart-flis-stitchen (`geo.py`) avviser en flis med urimelige dimensjoner
  (>40 Mpx) før dekoding — vern mot en ondsinnet/kompromittert flis-server (dekompresjonsbombe).
  (K2 dimensjon var alt løst.) Alle flatene er admin-gated. Verifisert: PNG/JPG/WEBP aksepteres,
  GIF/BMP avvises; trygg tema-verdi beholdes mens bilde-/ekstern-funksjoner blokkeres.
- **G-309 (OSPF/LSP-backend: robusthet):**
  - **P1 (HIGH):** en DB-feil under OSPF-discovery eller LSP-bygg kunne la en **session-advisory-lås
    henge** på en pooled connection (unlock-SELECT-en feilet i en abortert transaksjon) → alle senere
    bygg rapporterte «busy» til prosessen ble restartet. Frigjøringen er nå lekkasjesikker: `rollback`
    (renser evt. abortert tx) FØR `pg_advisory_unlock`, i både `run_discovery` og `run_lsp_build`; ny
    `_Cfg.rollback()` i `db.py`. Verifisert: lås tatt → tx abortert → rollback+unlock → låsen er fri igjen.
  - **P5a:** `ospf.run.n_errors` var hardkodet 0 → en delvis innsamling så ut som en ren kjøring. Nå
    registreres det ekte antallet hoster som ikke svarte på SNMP (`n_failed`) i `n_errors`.
  - **P3:** `a_if_ip`/`b_if_ip` lagret naboens (motsatt ende) IP på begge ender. Nå lagrer hver ende sin
    egen lokale interface-IP (fra /24-matchen). Feltene vises ikke i UI ennå, så dette var usynlig, men
    er nå korrekt lagret for fremtidig visning.
  - **P4-min:** fallback-OSPF-området ble plukket ikke-deterministisk fra et Python-`set`
    (hash-randomisert pr. prosess) → adjacency vandret mellom område-kart ved hver restart på
    fler-område-routere. Nå deterministisk (`min(...)`). (Kun v2; v3 + den dypere ipAddrTable-fiksen = G-344/G-343.)
  - **P7:** fjernet en død `reconcile_area(full=True)`-gren (full re-layout via direkte DB-skriving) +
    ubrukt `seed`-param + rettet villedende docstring — funksjonen finnes (bedre) i `compute_logical_layout`
    («plasser uten å lagre», G-227/G-233); direkte-skrivingen var uansett feil under den modellen.
  - Vurdert, ingen endring: P2 (link «up» krever begge naboer «full» = bevisst konservativt), P6
    (`ospf.lsp`-unik = dekket av G-310). Utsatt til egne saker: v3-feilmaskering + v3-område → G-343;
    ekte interface/område-deteksjon (erstatt /24-heuristikk) → G-344.

- **G-294 (shell-injection):** `restore.sh` interpolerte backup-filnavnet inn i en
  `sh -c "…tar xzf '/b/$b'…"`-streng som kjører root med data-volumet rw — et filnavn med apostrof
  kunne bryte ut og kjøre vilkårlig kode ved restore. Filnavnet sendes nå som **posisjonelt argument**
  (`$1`) til den indre `sh` (ren data, aldri kode). Completeness-scan bekreftet ENESTE injection-sted.
- **G-299 (IDOR / manglende scope-verifisering):**
  - K1: `create_link` verifiserer nå at nodene er `map_node` på DETTE kartet + at interfacet finnes —
    ikke stol på rå id-er fra klienten (håndlaget POST kunne referere fremmede noder / vilkårlig interface).
  - K2: infoscreen slide-mutasjoner (`remove`/`move`/`set_duration`) scopes til `screen_id`
    (`AND screen_id=%s`) — write på skjerm A kan ikke lenger mutere en slide på skjerm B.
  - K3: `_maps_guard` gjør ACL-avgjørelsen FØR draft-status-proben → lekker ikke kart-eksistens/
    publisert-status (409 vs 403) til uautoriserte. (K4 policy-inkonsistens = egen sak G-341.)
- **G-300 (rate-limiting):**
  - K5/K8: rate-limit-telleren ligger nå i DB (`rate_event`) — **delt/global** på tvers av gunicorn-
    workere (var per-worker → grense × antall workere) og ryddes periodisk av worker-en (ikke «>1000»).
  - K6: kilde-IP for throttle/audit kan ikke forfalskes via `X-Forwarded-For` forbi betrodd proxy —
    `_client_ip()` stoler kun på XFF når den direkte peeren er en betrodd proxy (`_PeerStash`-middleware).
  - K7: passkey-registrering + passord-bytte (bruker + admin) har nå rate-limit (hadde bare login).
  - Hotfix (felttest): (A) blokkerte innloggings-forsøk **logges** nå — throttlet via `Backoff`
    (~1/2/5/10 min m/ antall) så de ikke kan DDoS-e loggen. (B) admin/break-glass-innlogging har
    **egen** rate-limit-bøtte fra bruker-innlogging → bruker-brute-force fra samme IP låser ikke ute
    admin-recovery-stien.

## 2.2.2 — kodereview-batch ferdigstilt som HELE saker (robusthet, tidssone, validering, secrets/frontend/container)

Hele kodereview-batchen (3 AI-review 2026-07-08) håndtert som komplette saker — ikke delfikser.

### Secret-håndtering, frontend-korrekthet & container-herding (G-298 + G-315 + G-318 + G-325)

- **G-298 (secret-lekkasje):** SNMP-community/passord ekkoes ikke lenger i skjulte HTML-felt på
  discover-siden — de stashes server-side (økta) og siden får `Cache-Control: no-store`; `manage.py
  set-admin-password`/`reset-user-password` tar ikke lenger passord som posisjonelt CLI-arg (alltid
  skjult getpass-prompt) så det ikke lekker via `ps`/shell-historikk. (Google-nøkkel + `.env`-i-backup
  bevisst uendret — jf. beslutning.)
- **G-315 (frontend/JS):** rettet lagret **selv-XSS** via passkey-navn (navn i data-attributt, ikke
  inline-JS); logisk område-boble navigerer nå riktig (`/logical/<area>`, ikke 404 fra custom-kart);
  mislykket Google-session-henting caches ikke lenger for alltid (retry neste tegning); passkey-login
  sjekker `resp.ok` før JSON-parsing (proxy-502 gir forståelig feil); flowmap-tooltip klampes på begge
  akser; «Bytt interface» sletter den gamle linken FØRST når en erstatning er valgt (ikke før → «Avbryt»
  mistet linken stille).
- **G-318 (container-herding):** webapp + poller kjører nå som **non-root** (`USER bifrost`, uid 10001);
  `.dockerignore` i begge bygg-kontekster (tests/ beholdes bevisst — Tier C kjøres i containeren).
- **G-325 (feilhåndtering):** verifisert allerede fullført — de 3 skadelige `except Exception` i
  nav-rendringen ble splittet (`psycopg2.Error` stille / ekte bug logges) i 2.2.0; 110 legitime står.

### Validering ved grensen + backstop (G-337 + G-338 + G-339)

Valider ved grensen, delt validator der samme felt skrives flere steder (fjerner drift), + global backstop.

- **G-337:** global `@app.errorhandler(psycopg2.DataError)` → ren **400** i stedet for rå 500 for
  ALL malformet input som når en DB-cast (dato/numerisk/timestamp) — backstop for hele klassen.
- **G-339:** delt `snmp.valid_creds()` — SNMP-versjon (1/2c/3) + auth/priv-proto mot allow-list +
  avvis kontrolltegn/null-byte i community/passord — på `discover`, `discover_save` og `update_host`.
- **G-338 (hele klassen):** `if_index ≥ 1` og `speed 0..max_interface_speed_mbps` (ny innstilling,
  default 10 Tbps) via utvidet `_int_or_none(lo/hi)`; delt `_valid_role()` mot `catalog.ROLES` på alle
  fire interface-skrive-ruter (fjerner rolle-drift); logisk kart pinnet krever nå BÅDE x og y satt
  (fikset TypeError-asymmetri mot fysisk); bounding-box avviser snudd/degenerert utsnitt; label/navn/
  caption kappes (logisk node-label, kart-navn, infoskjerm-navn/caption); infoskjerm-visetid klampes
  til 2..300 s; node-antall pr. kart bundet av ny innstilling `max_map_nodes` (default 100); ACL-grants
  lages kun for kart/infoskjermer som faktisk finnes (ingen phantom-grants).

### Tidssone-konsistens (G-307)

Fullfører G-152-intensjonen: **all tidsvisning følger appens valgte tidssone**, data forblir
sone-nøytralt lagret (UTC). Tz-survey fant 8 steder (N1–N8) som fremdeles viste browser- eller
container-lokaltid; alle rettet.

- **Frontend (browser-tz → app-tz):** fysisk kart «data utdatert siden …»-banner (N1), tidsreise-
  slider-etikett (N2), og kart-kredittens tidsstempel (N3) bruker nå `window.APP_TZ` (samme mønster
  som grafene alt gjorde).
- **Backend (container-tz → app-tz):** footer-år regnes i app-tz (N4); nytt Jinja-filter `apptime`
  brukt på Min side sesjons-/passkey-tider (N5), outlier-sample-tid (N6), kartversjon-tid (N7) og
  infoskjerm-`created_at` (N8c).
- **Lagring (N8a/N8b):** `composer.map.created_at`/`updated_at` var TEXT (fryst streng i skrive-tidens
  tz) → migrert til `timestamptz` (idempotent, kun når fortsatt TEXT; eksisterende verdier bevares),
  slik at også kart-listenes tidsstempler kan tz-justeres ved visning.

### Robusthet for eksterne kall (SNMP + HTTP): poller, on-demand, gate & timeout-vakt (G-312 ⊕ G-336)

Hele **G-312 ⊕ G-336** tatt som én sammenhengende robusthet-epic.
Utløst av en poller-/ekstern-kall-kræsj 2026-07-09; en full scan fant at KRASJ-klassen var dekket men
HENG-klassen ikke var det (ingen timeout på SNMP/DNS/eksterne kall; synkrone eksterne kall frøs tråder).
Todeling: polleren (kjernen, egen rytme, IP-only) selv-vakter; on-demand (admin) gjenbruker gate +
delt timeout-mønster.

**Poller (kjerne-rytmen):**
- **Per-host timeout** (`POLL_HOST_TIMEOUT`, default 60 s) rundt hver `poll_host` + `gather(return_exceptions)`
  → en treg/hengt host feiler-fort og fryser ikke syklusen; all data kommer innenfor 5-min-intervallet.
- **Varbind-chunking** (`SNMP_MAX_OIDS_PER_GET`, default 60) på BEGGE GET-stegene (tellere + operstatus)
  → interface-rike hoster treffer ikke lenger `tooBig`/«too many varbinds» (falt tidligere helt ut).
- **IP håndheves** (poller SNMP-er kun mot IP, aldri hostname → ingen DNS-heng): validert både på input
  (add/edit host + discover) og ved bruk (poller + `snmp.discover` før `UdpTransportTarget.create`).
- Demo-telleren ryddes nå mot inventaret (ubegrenset vekst fjernet); `statement_timeout` på inventar-
  spørringen gir pen skip i stedet for «cycle error».

**On-demand (admin: discover, re-skann, OSPF/LSP-bygg, NetBox):**
- **`snmp.discover` totalbudsjett** (`DISCOVER_TIMEOUT`, default 100 s < gunicorn 120) → patologisk host
  gir pen feil i UI i stedet for at gunicorn SIGKILL-er worker-en.
- **Enkelt-host-refresh** kjøres nå via bakgrunns-worker-en (enqueue, scope=host-id) i stedet for synkront
  i request-tråden.
- **NetBox-sync (G-336-kræsjen):** kjøres nå i **bakgrunns-worker-en** (samme kø som re-skann/bygg) —
  operatøren starter den og kan forlate siden (status-poll viser fremdrift/resultat). Selve robustheten
  ligger i sync-funksjonene: fail-fast ping FØR per-host-løkka (uårbar NetBox → øyeblikkelig pen feil i
  stedet for N×timeout-akkumulering), samlet tidsbudsjett (`NETBOX_SYNC_BUDGET`, 90 s, idempotent «kjør
  igjen for resten»), **HTTP-cooldown** mot hamring, og **advisory-lås** så kun ÉN sync kjører om gangen.
  Poller/worker/kart rører aldri NetBox → NetBox-nede påvirker kun de admin-handlingene, med pen melding.
- **Ny generell HTTP/ekstern cooldown-gate** (`http_claim` + `external_claim`-tabell + innstilling
  «Ekstern-HTTP-cooldown») — søster til SNMP-cooldownen, for NetBox «og slikt».
- **Bygg-jobber** (OSPF/LSP) heartbeater nå underveis → et langt bygg blir ikke feilaktig regnet som dødt
  og re-køet (dublett som hamrer routerne).
- Periodisk poller-re-skann dedupes nå KUN mot andre re-skann (`kind='rescan'`) → et køet bygg sulter
  den ikke lenger.

## 2.2.1 — quick-wins fra kodereview (trivielle MEDIUM-delfikser)

Utvalgte lav-risiko delfikser fra review-batchen.

- **G-298:** `account_passkey_verify` returnerer nå kun exception-TYPE til klient (ikke full intern
  melding); full detalj beholdes i logg (som login-verify).
- **G-304:** `_win` (graf-tidsvindu) og `admin_outliers?thr=` avviser/klamper NaN/inf → ingen
  ubehandlet `to_timestamp`/`OverflowError`-500.
- **G-307:** `peak_trend` x-akse regner midnatt i KONFIGURERT tidssone (ikke UTC) → ingen
  dags-forskyvning vest for UTC; `queries._tz()` fikk samme regex-guard som `db._configured_tz`.
- **G-312 (del):** re-skann hopper over myk-slettede hoster (`deleted_at`-filter, begge stier);
  SNMP-discover klamper admin-oppgitt timeout/retries (≤10 s / ≤2) så stor verdi ikke blokkerer worker.
- **G-313:** NetBox — `netbox_id` oppdateres med COALESCE (ingen null-vask); device/site-id valideres
  som heltall før URL-interpolering (SSRF-vern).
- **G-315:** public logisk kart skjuler nå OSPF-metric (cost-chips) by default — regresjon av G-276
  (`showMetric=false` gjaldt kun når toggle-knappen fantes).
- **G-318 (del):** fjernet ubrukt `PROXY_COUNT` fra poller-servicen i compose.

## 2.2.0 — bugfix/review-fikse-release (kodereview 2026-07-08)

Basert på tre uavhengige AI-kodereviews av 2.1.2.

- **Kodehygiene / død kode (G-326/327/329/330/331):** fjernet ubrukt `_DAY_BUCKET` + stale
  kommentar og dupliserte `time`-re-imports i `queries.py`; fjernet ubrukte imports `auth`
  (maps.py) og `session` (ospf.py); fjernet døde front-end-hooks (`linkColor`, `expanded`-state,
  ubrukt CSRF-konstant); rettet `manage.py` shebang (linje 1) + konsoliderte doble versjons-headere;
  `catalog.VIEW_ROLES` aliaser nå `queries.OUT_OF_NETWORK` (ingen duplisert rolleliste).
- **Open-redirect-herding (G-303):** `_safe_next` (viderekobling etter innlogging) validerer nå
  strengt at `next` er en intern sti — parser + dekoder og avviser scheme/netloc og kodede
  `//`-varianter (`%2f%2f`, `\/\/`, kodet backslash). Lukker hele open-redirect-klassen.
- **Classmap-korrekthet (G-308):** seed-spørringen (fersk installasjon) henter nå `role` og `asn`
  fra SAMME interface-rad (DISTINCT ON) i stedet for `min(role)`/`max(asn)` separat — unngår
  forslags-regler som aldri fantes (f1); `_fresh_cache` bruker double-checked skriving så en
  tregere tråd ikke overskriver en nyere cache (f3). (Samtidig-skriving/500 = egen sak G-323.)
- **Feilhåndtering strammet inn (G-325):** 7 for brede `except Exception` i nav-/render-/grant-stien
  fanget alt stille — en ekte bug kunne degradere nav-baren usynlig. Nå fanges DB-nede spesifikt
  (`psycopg2.Error`, stille fallback som før), mens andre feil LOGGES i stedet for å svelges
  (nav-verdener, «Kart ▾»/«Logisk ▾»-dropdowns, branding, ACL-nav-flagg, area-grant-UI, tema-tokens).
  De øvrige ~110 brede fangerne er bevisst DB-nede-resiliens og er urørt.
- **Backup/restore-scripts robusthet (G-317):** `backup-cold.sh` restarter nå stacken på enhver exit
  (trap) — en feilet tar lar aldri Bifrost stå nede; retention er count-basert (behold 10, env
  `RETENTION_COUNT` erstatter `RETENTION_DAYS`), og `backup-hot.sh` rydder forlatte `.dump.partial`
  (feilede dumps hopet seg opp og fylte disken); `restore.sh` uten args viser nå HELE hjelpeteksten
  (inkl. «DB_PASSWORD må matche kilden»-advarselen som ble kuttet).
- **Samtidig skriving / UniqueViolation-500 (G-323 + G-321):** global `IntegrityError`/`UniqueViolation`-
  errorhandler gir nå ren 409 «finnes allerede» i stedet for rå 500 — backstop for HELE klassen +
  fremtidig kode. De fire racy admin-skjemaene (classmap-regel, ny bruker, host-oppdag, host-endre) har
  i tillegg inline `try/except` som beholder den pene inline-feilboksen. `discover_save` bruker nå SAMME
  case-insensitive clash-sjekk (navn ELLER adresse) som `update_host` (G-321) — de kan ikke lenger
  drifte fra hverandre og slippe gjennom duplikat-hoster.
- **Demo-data-sikkerhet (G-319):** nytt additivt `is_demo`-flagg på host. `seed-demo` NEKTER nå på en
  installasjon som har ekte (ikke-demo) hoster — demo lastes kun i en tom installasjon; demo-hoster
  merkes og får en ikke-standard community (`demo-mock-only`, ikke `public`); «Demo-backbone»-kartet
  bygges KUN fra demo-hostene (ikke lenger fra ALLE enabled hoster) så ekte infrastruktur aldri blandes
  inn i demo-kartet. (Demo beholdes bevisst — nyttig for å teste kart-motoren.)

## 2.1.2 — passkey challenge-race fikset

- **Fiks:** conditional-autofyll og «Logg inn med passkey»-knappen hentet hver sin
  innloggings-challenge og overskrev hverandre i sesjonen → intermitterende
  «challenge was not expected challenge» (tilfeldig mislykket passkey-innlogging, sett i
  felttest på mobil). Nå hentes options ÉN gang og deles; knappen avbryter det ventende
  conditional-kallet (AbortController) i stedet for å starte et konkurrerende.

## 2.1.1 — passkey-felttest-herdinger

- **CSRF-fiks:** passkey-kallene fra login-siden ble avvist (403) — inline-scriptet kjørte
  før den globale fetch-wrapperen; passkey.js setter nå X-CSRFToken selv.
- **Skjul passkey der det umulig kan virke:** klientside-deteksjon (secure context +
  hostnavn-ikke-IP) — login-siden holdes helt ren, Min side forklarer årsaken («krever
  HTTPS», «krever hostnavn — ikke IP-adresse»). Rå IP kan aldri få passkey (RP ID må
  være et domene) — ingen app-modus kan overstyre nettleseren.
- Prefiks-fiks: alle passkey-URL-er via url_for (virker bak /bifrost/-stier); feilmeldinger
  beriket med feiltype + forventet rp_id/origin i både UI og logg.

## 2.1.0 — passkey/WebAuthn: innlogging med ett trykk (Gitea #14)

- **Passkey-innlogging (Gitea #14):** FaceID/fingeravtrykk/Windows Hello/PIN/security key —
  brukernavn-løst med ett trykk; avbryt gir vanlig passord-innlogging. Registrer/administrer
  passkeys på Min side (navn, sist brukt, synk-merkelapp, «gammel»-flagg). Helt in-house
  (py-webauthn lokalt); break-glass-admin er bevisst uten passkey. Se `spec-v2.1.0-passkey.md`.

## 2.0.0 — gruppe-basert tilgang, sentralt ACL-register, server-side økter (MAJOR, G-285)

- **All tilgang henger nå på GRUPPE, aldri på enkeltbruker.** Tre faste systemgrupper:
  **brukere** (default — logger inn, ser ingenting før gruppen gis tilgang; kan aldri få
  skrive), **operatør** (ser alt inkl. historikk/logg/hostliste/LSP-OSPF; endrer som
  utgangspunkt ingenting) og **admin** (alt — det delte admin-passordet består som
  break-glass). Én bruker = én gruppe. Custom-grupper med valgt baseline (deny/read) er
  bygget inn, men UI-et for å opprette dem er bevisst skjult i 2.0.0.
- **Sentralt ACL-register (`acl.py`)** — ÉN regel alle features spør: admin → alt; hard
  operatør-sone (ikke grantbar for brukere); låst kart → maks lese; per-objekt-oppføring
  (ingen/lese/skrive — også vekk-valg som tar noe BORT fra operatør); ellers gruppens
  baseline. Grants redigeres per gruppe under Admin → Grupper (kategorier/kunder/logisk +
  per-kart og per-infoscreen).
- **Operatører kan opprette kart** (fysiske og logiske) — skaperens gruppe får automatisk
  skrive på det nye kartet; skrive-tilgang kan ellers gis per kart. Ghost-noder og nye
  hoster er fortsatt admin. Infoscreen-oppsettet er operatør-flate (kan velges vekk per
  skjerm).
- **Kart-lås:** admin kan låse et kart (🔒 i kartlista) — read-only for alle utenom admin
  til det låses opp; tildelte skrive-tilganger ligger urørt gjennom lås/opplås.
- **Server-side økter:** innloggings-økter er nå rader i databasen og kan revokeres —
  deaktivering/passord-reset logger brukeren ut UMIDDELBART på alle enheter. «Min side»
  viser egne innloggede enheter med «logg av alle andre». Økt-levetid etter gruppe:
  brukere `USER_MAX_DAYS` (7 d), operatører `OPERATOR_MAX_DAYS` (2 d), admin som før.
- **Automatisk, tapsfri migrering:** eksisterende view-brukere får hver sin (skjulte)
  gruppe som bærer nøyaktig dagens tilganger — ingen mister noe. Idempotent, kjørbar
  flere ganger. **NB (major):** alle innloggede økter må logge inn på nytt én gang.
- **Audit:** innlogging OK/feil (med IP), utlogging, deaktivering, gruppeflytt,
  tilgangs-endringer, kart-lås, passord-reset — alt i Loggen (`log_event`).
- **CI:** ny gating-test (35 sjekker) for hele tilgangsmatrisen (spec §11), inkl.
  G-279 (graf-zoom følger graf-scopet, ikke admin-lås).
- **Parity-prinsippet (generell regel):** info og utseende er LIKT på tvers av roller —
  tilgangen styrer kun hvilke handlinger som vises. Inventar-siden er admin-hostlisten i
  lese-modus (samme mal/datakilder, interfacelister inkludert), og `/inventar/kategorier`
  er admin-kategorisiden i lese-modus. About viser drift-status (app/db/poller) til alle;
  tallene er operatør-sone.
- **UI speiler tilgang:** Rediger-/lifecycle-knapper på kart (fysiske OG logiske) vises kun
  der gruppen faktisk har skrive-tilgang på akkurat det kartet; public-lenken («kart-only»)
  vises for alle som kan åpne den; infoscreen-knapper følger per-screen-tilgang; pen norsk
  «Ingen tilgang»-side erstatter rå server-feil ved direkte-URL.
- **Ryddigere meny:** kjernen (Internett/Backbone/Fordeling/Kart/Logisk) alene på toppen;
  Historikk/Inventar/Logg/Infoscreens/Min side samlet i «Verktøy ▾»; verktøy-snarveier
  øverst på Min side. «Min side» viser innloggede enheter med kompakt nettleser/OS-etikett.
- **Felttest-fikser:** operatør kan nå redigere/publisere/parkere sine EGNE logiske kart
  (rutene var admin-låst); logisk GPS-info på lese-nivå (bundet til kartets medlemmer);
  nye kart på oppgraderte installasjoner fikk mørk bakgrunn (gammel kolonne-default) —
  flippet til gråhvit; kart-listens Lås/Lås opp-knapp med stabil bredde.
- **Kart-fiks (G-293):** default-størrelsen contain-fitter nå HELE vinduet — X/Y-forholdet
  bevares og verken bredde eller høyde går utenfor skjermen (før fylte kartet bredden og høye
  kart rant under vinduskanten). Re-fitter ved vindus-resize; gjelder fysisk + logisk kart.
- **Egen brukeradmin-side:** Admin → «Brukere & grupper» (`/admin/users`) — kontoer,
  gruppe-flytting, passord-reset, deaktivering og inngangen til tilgangs-editoren per
  gruppe, på én dedikert side (admin-forsiden lenker dit). Fikset samtidig at gruppe-
  tabellen/flytt-valgene på den gamle innebygde seksjonen sto uten data.
- **Kryssjekk-audit (4 uavhengige gjennomganger av hele koden):** tettet at en anonym
  infoscreen-token kunne fortsette å hente live-rater for et senere AVPUBLISERT kart
  (data-ruta manglet publisert-vakten visnings-ruta hadde); admin-GRUPPE-medlemmer fikk
  feilaktig 404 på draft-forhåndsvisning; live-kartbakgrunn (Google) sviktet stille for
  lese-brukere; /about-tallene er nå operatør-sone; «kun se oppsett»-vekk-valget for
  infoscreens gir nå en faktisk lese-side; død per-bruker-tilgangskode fjernet
  (kart-tilgang settes per gruppe).

## 1.8.4 — kartene skalerer også OPP på store skjermer (G-291)

- **Fiks (G-291): kart-visningene (fysisk + logisk) vokste aldri forbi den definerte kart-størrelsen.**
  SVG-kartet er vektor og skalerer fritt, men innholds-kolonnen var kappet på 1100 px — så kartet
  skalerte ned på små skjermer, men aldri opp på store. Kart-visningene bruker nå nesten full
  nettleser-bredde (96 vw); alle andre sider beholder dagens bredde.
- **Fiks: node-etiketter ved kartkanten rant utenfor rammen** (fysisk kart, både visning og
  redigering). Etiketter klampes nå innenfor kartet horisontalt, og en etikett som ville truffet
  bunnen legges over noden i stedet; motpart-etiketter klampes mot toppen.
- **Fiks: redigeringsmodus på fysisk kart hadde fortsatt hvit tekst** (fra den mørke æraen) — nå
  mørk tekst og lys node-ring som i visningen, lesbart på den nye lyse bakgrunnen.

## 1.8.3 — infoscreen: bla med trykk + kiosk-kart viser alt (G-290) · Datakvalitet-fiks

- **Fiks (G-288, prod-funn): Topper-vedlikeholdet tålte ikke prod-skala.** Reparasjonens re-fold
  kjørte hele historikken og sprakk på databasens 30-sekunders spørringsgrense («statement timeout»)
  → feilmelding + hull i Topper for dagen. Nå: folden kjører uten spørringsgrense (som aggregat-
  backfillen), og reparasjonen re-folder kun et målrettet vindu rundt den reparerte dagen (sekunder).
- **Ny (G-288): «Rens Topper»-knapp på Datakvalitet.** Gamle falske all-time-rekorder (fra før
  filteret) blir stående til de renses — Topper senker aldri av seg selv. Knappen fjerner umulige
  rekorder (> 2 Tbps) og bygger Topper på nytt fra filtrerte data.
- **Ny (G-288 fase 2): fysisk umulige rater vises ALDRI — i noen visning.** En rate over 2×
  interface-kapasiteten (eller 2 Tbps der kapasitet er ukjent) er fysisk umulig (teller-glitch eller
  ifIndex-omstokking på ruteren) og blir nå et ærlig **gap** i stedet for en gigantspike — i alle
  grafer (rå og aggregat), weathermap («ingen data»), per-interface-historikk og Topper-foldingen
  (falske all-time-rekorder kan ikke lenger oppstå). Ikke-destruktivt: rådataene røres ikke, og
  Datakvalitet-siden ser fortsatt anomaliene så de kan repareres/slettes. Dette løser også tilfellet
  der telleren hopper VARIG (ny base) — der finnes det ikke noe «riktig tall» for overgangs-
  intervallet, og gap er det korrekte svaret.
- **Fiks (Datakvalitet/G-288): reparasjonen virket ikke mot ekte data.** Finneren oppga tidspunkt i
  hele sekunder, men ekte poller-lesinger har mikrosekunder → oppslaget «fant ikke lesingen». Nå slås
  den eksakte lesingen opp i et 1-sekunds vindu (lesinger er 5 min fra hverandre — entydig) og
  repareres med garantert treff. (Demo-data hadde hele sekunder — derfor slapp den gjennom testen.)
- **Ny (G-290): trykk hvor som helst på en infoskjerm → neste bilde.** Bla deg kjapt gjennom en lang
  loop; rotasjons-timeren starter på nytt fra bildet du landet på.
- **Fiks (G-290): kart-slides på infoskjerm viser nå HELE kartet tilpasset skjermen** — auto-innzoomen
  fra 1.8.2 (ment for interaktiv mobil-visning) gjorde kiosk-kartet større enn skjermen uten mulighet
  til å panorere. Kiosk-slides dropper pan/zoom og zoom-knappene helt.
- **Bevisst beslutning (G-290, for fremtidig referanse):** innholdet på en infoskjerm er REN VISNING —
  det er ikke mulig å interagere med det som vises (zoome, panorere, klikke i kart/grafer). Eneste
  interaksjon er trykk → neste bilde. Vil du utforske et kart, bruk selve appen (som har pan/zoom).

## 1.8.2 — Datakvalitet: finn og reparer outliers (G-288)

- **Ny: Admin → Datakvalitet (G-288).** En teller-glitch eller ifIndex-omstokking på en ruter kan gi
  ÉN lesing med et absurd hopp → falsk gigantspike (f.eks. 240 Tbps) som forgifter grafer og Topper
  (`GREATEST(delta,0)` fanger bare tellere som går BAKOVER — ikke umulige hopp fremover). Den nye
  siden finner sample-par over en terskel (default 1 Tbps, justerbar) og reparerer kirurgisk per
  lesing: **«Rett (snitt)»** setter tellerne til snittet av lesingen før/etter (totalvolumet bevares),
  eller **«Slett lesing»**. Begge re-heler aggregatene rundt tidspunktet og renser + re-folder Topper
  (peak_daily er GREATEST-upsert og ville ellers husket den falske rekorden for alltid). Alle
  reparasjoner logges i Loggen.
- **Ny: trykk på en node gir info-popup — på BEGGE karttyper.** Logisk kart: alle noder viser nå
  samme info som hover ved trykk (før kun ghost); «Adopter» vises fortsatt kun på ghost (admin).
  Fysisk kart: node-trykk viser host-info med **«Åpne host i Admin»**-link (hopper til hosten på
  admin-sida; kun admin — public er uberørt). Popup-en er delt kjerne-funksjonalitet og holdes
  alltid på skjermen.
- **Ny: kartnavnet på public-kartet lenker tilbake til den innloggede visningen** av samme kart
  (ikke på infoscreen-slides — kiosken forblir ren visning). Tittelen fikk også lesbar farge på den
  nye lyse kartbakgrunnen (var stylet for den gamle mørke).
- **Endret: link-modalens tittel bruker eksakt routernavn.** Kartet og hover bruker visningsnavn
  (som før), men modalen er en drill-down — tittelen viser nå hostens faktiske navn i stedet for
  visningsnavnet.
- **Tilbake: hover-linja øverst i link-modalen.** Den ene linja med øyeblikks-retning + rate (fjernet
  i 1.8.1-ryddingen) er tilbake — det er den som gir touch-brukere øyeblikks-tallet. Retningsbevisst
  graf beholdes.
- **Endret: Trafikkflyt-overlegget er nå BETA og skjult som standard (G-289).** Funksjonen er ikke
  god nok ennå. Knappen på weathermap er borte til admin slår den på under Admin → Innstillinger
  («Trafikkflyt-overlegg på weathermap (BETA)») — da vises den som «Trafikkflyt (BETA)».
- **Justert: kart-bakgrunnen er nå gråhvit (#f0f1f7) i stedet for ren hvit** — ren hvit var for skarp
  og gikk i ett med kort-rammen (felttest). Kart som står på en av de tidligere standardfargene
  (mørk eller 1.8-hvit) flippes automatisk; egendefinert bakgrunn røres aldri.

## 1.8.1 — retningsbevisst link-graf (Gitea #12-oppfølging)

- **Forbedret: trykk på en link-halvdel fremhever RETNINGEN i grafen.** Grafen i link-modalen viste
  det samme uansett hvilken halvdel du trykket — nå startes serien for retningen du trykket PÅ, og
  motsatt retning «av» (slås enkelt på igjen med Inn/Ut-knappen i forklaringen under grafen).
  Retningsvalget nullstilles per link-åpning; bytter du tidsvindu i modalen beholdes valgene dine.
- **Fjernet: info-feltet i link-modalen (fra 1.8.0)** — det dupliserte node/interface/kapasitet som
  allerede står i modal-tittelen. Tittelen + den retningsbevisste grafen dekker nå det samme, uten
  dobbel informasjon.
- **Endret: generiske standardverdier.** Default nett-navn er nå **«Network»** og default ASN
  **AS65432** (privat ASN-område) i stedet for de tidligere innebygde verdiene, og default-logoen er et enkelt,
  generisk **router-ikon** (in-house SVG). Egne verdier settes som før i `.env` (`OUR_NAME`/`OUR_ASN`)
  eller på Admin → Innstillinger (navn/ASN/logo — databasen vinner over env).

## 1.8.0 — mobilvennlig: responsiv app, lyse kart med pan/zoom (Gitea #12)

- **Ny: responsiv layout (mobil → PC).** Menyen kollapser til en hamburger på små skjermer (med
  undermenyene Kart▾/Logisk▾ inline — hover finnes ikke på touch), chrome/kort krymper, grafene får
  tilpasset høyde (mobil ~200px, desktop uendret ~320px), og brede tabeller (Topper/admin) scroller i
  sin egen boks — aldri sidelengs-scroll på siden.
- **Ny: lys kartbakgrunn som standard.** Dagens mørke standard (`#0d132d`) var for mørk/utydelig —
  nye kart får hvit bakgrunn, og eksisterende kart som fortsatt sto på den gamle mørke standarden
  flippes automatisk til hvit ved oppgradering (egendefinert bakgrunn røres aldri; ikke-destruktivt).
  Node-tekst/ringer på fysisk kart fulgte med til lys-tema (logisk kart var allerede lys-vennlig).
- **Ny: panorer + zoom i kart-visningen.** +/−/⟲-knapper (40px tap-mål), pinch på touch og dra-for-å-
  panorere når innzoomet — ren viewBox-transform i den delte kart-kjernen, så det gjelder både fysisk
  og logisk kart. På smal skjerm starter kartet **innzoomet i lesbar størrelse** (panorer rundt) i
  stedet for å krympes til uleselig. Vanlig side-scroll bevares når kartet ikke er zoomet.
  (Dette er visnings-zoom — ikke site-aggregering/LOD.)
- **Forbedret: tooltips og popups holdes alltid PÅ skjermen** — klampes mot kart-rammens kanter
  (før kunne de renne utenfor, særlig på mobil og nederst i kartet).
- **Forbedret (touch): trykk på en link viser nå samme info som «hold musa over»** — øverst i
  trafikk-modalen, over grafen. Hover er uendret på desktop.
- **Endret (touch-vennlig): trykk på en ghost-node** (umonitorert nabo, logisk kart) viser nå node-
  infoen med **«Adopter»-valget øverst** i en liten popup — i stedet for å navigere direkte til
  adoptering (på touch fantes aldri hover-hintet, så et trykk adopterte «blindt»). Hover er uendret.
- Grafene beholder uPlot-standard oppførsel på touch (tap = verdier, dra = tids-zoom) — felttestes på
  mobil.

## 1.6.1 — stillere poller-oppstart på fersk installasjon (G-283)

- **Poller (G-283): ingen «relation … does not exist»-støy ved aller første oppstart.** På en fersk, tom
  installasjon kunne polleren starte før webappen hadde rukket å opprette config-tabellene og logge noen
  ERROR-linjer (`host`/`log_event`/`rescan_job` finnes ikke) før den ventet én syklus. Oppstarts-gaten
  venter nå også til config-skjemaet faktisk finnes før første DB-berøring. Rent kosmetisk — ingen data
  ble påvirket, og eksisterende installasjoner så aldri dette.

## 1.6.0 — raskere grafer: forhåndsberegnet 7d + fri graf-zoom (G-281/G-278/G-261)

- **Ytelse (G-281): 7-dagers-grafen forhåndsberegnes nå.** 7d regnet før rate fra rådata ved hvert
  kall (~480k rader, ~1,5 s, og falt utfor en minne-klippe ved mange samtidige seere). Den leser nå
  1-times-aggregatet — samme tall, men ~12× raskere (~120 ms) og uten minne-presset. 36h beholder full
  5-minutters oppløsning (rå); 7d/30d er 1-times snitt.
- **Ytelse (G-281): lange visninger (12/24/36/48 mnd) leser 1-døgns-aggregatet direkte.** Den gamle veien
  regnet hele års-serien om ved spørretid (mot ~22 s + stor disk-bruk når et helt år hadde samlet seg).
  1-døgns-aggregatet er nå **tidssone-bevisst** (korrekte døgngrenser i valgt sone, som før) og bygges om
  automatisk hvis tidssone-innstillingen endres.
- **Ny: lang-visning som nedtrekk.** «12 mnd»-knappen er nå et nedtrekk med **12 / 24 / 36 / 48 mnd**
  (default 12). Alle på 1-døgns snitt. Grafene viser også hvilken **oppløsning** de er på (liten etikett
  ved knappene: «5-min snitt» / «1-time snitt» / «1-døgn snitt»).
- **Ny (G-278): zoom i grafene.** Dra og marker et område i en graf for å zoome inn — grafen henter da
  **finere oppløsning** for akkurat det tidsrommet hvis den finnes (ned til 5-min for de siste 90 dagene),
  eller den fineste som er igjen for eldre perioder. Dobbeltklikk for å gå tilbake. Gjelder Internett-,
  Backbone- og per-interface-historikk-grafene (Fordeling har sin egen nå-orienterte visning).
- **Kart (G-261): tydelig melding når «eksakt GPS» mangler geo-bakgrunn.** «Eksakt GPS-plassering» krever
  et geo-referert bakgrunnskart; uten ett falt plasseringen stille tilbake til relativ og så ut som
  «relativt» uten forklaring. Nå sier kartet fra: «Eksakt GPS krever geo-referert bakgrunnskart — plasserte
  relativt i stedet» (fysisk kart).

## 1.5.8 — Fordeling «Nå» i stedet for 36-timers snitt (G-277)

- **Bug (G-277): Fordeling viste 36-timers snitt i stedet for nåværende trafikk.** Fordeling (egonett)
  hadde «arvet» de historiske range-ene fra Internett/Backbone (36t/uke/mnd/12mnd) og midlet inn/ut-bps
  over hele vinduet — så en peaky CDN (f.eks. Akamai ~44 Gbps NÅ over PNI) viste 36t-snittet (~7,8 Gbps)
  og så ut som en regnefeil. Fordeling er en NÅ-orientert visning, så range-velgeren er nå **Nå / 1 t /
  24 t** (default **Nå**). «Nå» = øyeblikks-rate (siste sample-par pr. interface, summert pr. node) — samme
  verdi-familie som weathermap, så Fordeling «Nå» matcher kartet. (Internett/Backbone/Topper beholder de
  historiske range-ene.)

## 1.5.7 — y-akse adaptiv + infoscreen visetid-edit + logisk metric av (G-273/275/276)

- **Feature (G-275): rediger visetid på en eksisterende infoscreen-slide.** Visetiden kunne kun settes
  når en slide ble lagt til; på slide-lista var den statisk tekst. Nå er den et redigerbart felt med
  «Lagre» rett på raden (klamp min 2 s).
- **Endring (G-276): logisk kart viser IKKE OSPF-metric (cost) by default.** Metric vises nå skjult ved
  innlasting; «Vis metric»-toggelen slår den på ved behov (reduserer info på skjermen). Fysisk kart har
  ikke metric og er uberørt.
- **Bug (G-273): y-akse-etikett ble klippet — første siffer kuttet.** Grafenes y-akse hadde en fast
  bredde (`size: 70` px); uPlot høyrejusterer tall-etikettene, så en bred etikett («80,0 Gbps») rant
  forbi venstre kant og klippet det mest signifikante sifferet. Trigget av nettleser-zoom / skjerm-DPI /
  systemfont (én Chrome-bruker så «8»-rest + «0,0 Gbps»; verste tilfelle er to-sifret-med-desimal, siden
  enheten dropper desimalen fra 100 og oppover). Fiks: y-akse-bredden er nå **adaptiv** — måler faktisk
  bredeste etikett (uPlots auto-size, DPR-korrigert til CSS-px) i stedet for en fast verdi → klipper
  aldri, uansett font/zoom/DPI eller tallstørrelse. Forward-kompatibel med den planlagte dynamiske
  skaleringen (G-274): endrer den fonten, følger gutteren med av seg selv.

## 1.5.6 — små-bugfixer (infoscreen-graf + skjult host «ingen data»)

- **Bug (G-271): infoscreen-grafene var stygge (mørk bakgrunn).** Graf-sliden (`screen_graph.html`)
  hardkodet mørk bakgrunn, mens hovedgrafene på Internett/Backbone sitter på det lyse kortet
  (`var(--surface)`). Nå bruker infoscreen-grafen samme lyse bakgrunn + mørk tittel via branding-
  tokenene (ingen harde farger). Grid, akser og serier brukte allerede de lyse tema-fargene, så
  bakgrunns-byttet gir hele det fine uttrykket.
- **Bug (G-272): skjult host viste «NEDE» på kart-linker i stedet for «ingen data».** Link-status
  sjekket sist kjente `oper_up` FØR ferskhet, så en skjult/ikke-pollet enhet (med stale `oper_up=False`)
  ble vist som «NEDE» for alltid. Nå avgjøres status av ÉN delt regel (`queries.link_state`) med
  **ferskhet først**: uten ferske data vet vi ikke noe → «ingen data», aldri «nede». «Nede» krever en
  fersk lesing som faktisk sier oper down (aktivt overvåket port). Gjelder både innlogget kart
  (`/api/maps/<id>/data`) og **public-kartet** — sistnevnte kunne før ikke vise «ingen data» i det hele
  tatt (slo down+nodata sammen). Logisk kart bruker OSPF-naboskap-state (ikke berørt).

## 1.5.5 — ett kart-system: logiske kart får fysiske karts draft/edit-livssyklus (G-266/267/268)

- **Logiske kart redigeres nå nøyaktig som fysiske kart.** Tidligere hadde logiske kart bare en
  *layout*-draft (node-posisjoner); medlemskap (legg til / fjern router / ghosts) **og bakgrunn** ble
  skrevet rett til det publiserte live-kartet — umiddelbart og permanent, utenom draft/Forkast. Nå får
  logiske kart — **både auto-area og custom** — den samme **efemere arbeidskopien (scratch-tvilling)**
  som fysiske kart: «Rediger» lager/gjenbruker en arbeidskopi, **alle** endringer (medlemskap, bakgrunn,
  layout, canvas) skjer der, arbeidet forsvinner hvis du forlater siden uten å lagre (samme backstop-
  opprydding som fysisk), «Lagre utkast» beholder det, «Aktiver» merger arbeidskopien → live (+ tar vare
  på forrige som en versjon), og «Forkast» er to-trinns (ulagret → lagret utkast → aktiv).
  - **G-266:** bakgrunn (live/snapshot) kan nå settes på logiske kart — den settes på arbeidskopien.
  - **G-267:** å legge til en host aktiverer ikke lenger kartet permanent; endringen skjer på arbeidskopien og «Forkast» angrer den.
  - **G-268:** et kart kan ikke lenger endres utenfor redigeringsmodus (mutasjon mot et publisert kart avvises).
  Implementert utelukkende på den logiske siden + to additive (logisk-only) linjer i den delte kart-
  motoren; **fysiske kart er uendret** (regresjons-verifisert). Begge logiske karttyper deler nå én
  render-/data-regel (`_graph_for_map` + `_logical_view_data`).
- **CI (G-269): syntetisk kart-gating-test.** Ny integrasjons-test (`webapp/tests/citest_map_gating.py`)
  som kjøres i CI Tier C (inne i den bootede stacken) og verifiserer at kart-MUTASJONER avvises i feil
  tilstand — bakgrunn/medlemskap mot publisert kart → 409, mutasjon som ikke-admin → avvist — for BÅDE
  fysisk og logisk, pluss at «Rediger»→arbeidskopi→Aktiver/Forkast holder live ren. Fanger klassen som
  slapp G-266/267/268 forbi (22 sjekker). Visuell korrekthet forblir manuell felttest.
- **Robusthet (G-270): `if_sample`-hypertabellen opprettes nå idempotent ved boot.** Tidligere ble
  metrics-grunnskjemaet (`if_sample` + hypertabell + indeks) kun opprettet av `db/schema.sql` ved fersk
  DB-init. Nå speiler `db.py` det ved oppstart (`ensure_metrics_schema`, bak boot-porten, før continuous
  aggregates) — appen er selv-helbredende og avhenger ikke av at schema.sql ble lastet (i CI/Tier-C
  bind-mountes den ikke inn). På eksisterende instanser er alt no-op (`IF NOT EXISTS`/`if_not_exists`).

## 1.5.4 — koordinert oppstart mot databasen (boot-port)

- **CI (Gitea Actions, in-house):** lagt til `.gitea/workflows/ci.yml` + enhetstester i `webapp/tests/`
  (`test_placement.py`/`test_snmp.py`/`test_geo.py`). Ved hver push til `review`/`main` kjøres syntakssjekk
  (Python `compileall` + JS `node --check`) og enhetstester/invarianter (plassering: ingen overlapp / ingen
  node-på-lenke / determinisme · `snmp.classify` · geo-projeksjon). Helt in-house — ingen markedsplass-actions
  / ingen github.com (manuell `git clone` fra git-serveren), repo-scoped runner. Fanger «rørlegger»-
  regresjoner; visuell felttest forblir manuell.
- **CI Tier C (integrasjon):** `.gitea/workflows/ci-integration.yml` bygger imagene + booter stacken
  (MOCK/DEMO, isolert `-p`-prosjekt + egen port) + treffer `/healthz` — fanger boot-/spørrings-/migrerings-
  regresjoner. Kjører IKKE på hver commit: manuelt + på push til `main` + på push til `review` kun når
  boot-kritiske filer (requirements/Dockerfile/docker-compose/schema/db.py) endres. Rydder med `down -v`.

- **Diagnostikk (G-264): bedre logging av re-skann-feil.** «re-skann feilet» kunne dukke opp på en host
  uten en nyttig logglinje om HVA som feilet. SNMP-discovery sier nå hvilket steg/OID som røyk (sysName,
  ifName, ifAlias, ifSpeed, ifOper, ifType) og hvor langt en walk kom før den feilet, i stedet for en bar
  feilmelding. Re-skann-feil logges nå som en greppbar `re-skann FEILET: <host> (<adresse>) — <type>:
  <detalj>`, og hver re-skann-jobb avslutter med en oppsummering av hvilke hoster som står med re-skann-feil.
  Lar oss skille en intermitterende SNMP-timeout fra andre årsaker (instrumentering — selve årsaks-fiksen
  tas når prod-loggen viser hva det faktisk er).

- **Bugfix (G-263): skjult (myk-slettet) host kunne legges til på kart.** Kart-host-listene (host-plukkeren
  på kart-lista, «+ legg til host» i editoren, auto-kart-byggets «alle hoster», og bytt-enhet-målet)
  filtrerte kun `enabled = 1`, ikke `deleted_at IS NULL` — så en skjult host dukket opp som valgbar og
  kunne legges til. Nå ekskluderes skjulte hoster fra alle disse listene, og `add_host_to_map` avviser en
  skjult/deaktivert host server-side (409) som ekstra vern. (Poller pollet dem allerede ikke — den
  filtrerer `deleted_at IS NULL`, G-189.)

- **Drift (G-262): boot-port — fjerner hele klassen av boot-tids DB-kappløp.** Ved oppstart slo flere
  prosesser (webapp-workere + poller + bakgrunns-tråd) mot databasen samtidig, så migreringer/DDL kunne
  deadlocke mot bakgrunnsjobbers lesing/skriving (samme rot som G-073/G-120/G-245, og deadlocken i G-259s
  TimescaleDB-oppgradering). Nå kjøres **all on-boot DB-migrering bak ÉN global port** (`db.bring_up()`,
  advisory-lås): kun én prosess migrerer av gangen, og bakgrunnsjobbene/poller **venter** på porten før de
  rører databasen. Resultat: ingen tunge spørringer kjører samtidig med migrerings-DDL → deadlockene
  forsvinner. Nye on-boot-operasjoner legges bare til i `bring_up()` og er automatisk beskyttet. Verifisert
  i test (fersk 2.17.2-volum → image-bump 2.28.0 → TimescaleDB-oppgradering kjørte rent, **0 deadlocks**,
  app frisk). Engangs-sesjonsforstyrrelsen fra `ALTER EXTENSION` (restart av TSDB-bakgrunnsworkere) logges
  nå som godartet info, og skjema-init fullføres av seg selv ved første forespørsel. Poller venter til
  TimescaleDB-extensionen faktisk er **i takt** med biblioteket (ikke bare til porten er fri) før første
  poll-sykel, så den ikke åpner langlevde tilkoblinger på et gammelt bibliotek som TSDB ellers ville
  resirkulert midt i drift.

## 1.5.3 — bugfikser fra felttest av 1.5.2

- **Drift (G-259): Bifrost holder selv TimescaleDB-extensionen i takt med imaget.** Når db-imaget bumpes
  til en nyere TimescaleDB (her 2.17.2 → 2.28.0-pg16) henger SQL-extensionen igjen på gammel versjon
  (db logget «the timescaledb extension is not up-to-date»). Bifrost kjører nå den offisielle, ikke-
  destruktive `ALTER EXTENSION timescaledb UPDATE` automatisk ved oppstart (db.py) — idempotent (no-op
  når alt er i takt), serialisert mellom workere, ikke-fatal. Ingen manuell ops, ingen nedlasting (selve
  binæren byttes kun ved image-deploy). **Deploy-merknad:** ta DB-backup før du deployer image-bumpen —
  SQL-migreringen er ikke-destruktiv men irreversibel, og gir en kort sesjonsforstyrrelse under kjøring.

- **Kart-editor (G-260 / G-253): «Forkast» er nå ett konsistent «steg tilbake» på BÅDE fysisk og logisk
  kart.** Før gjorde knappen to forskjellige ting: fysisk re-lastet sist lagrede utkast (forkastet aldri
  draften), logisk slettet draften med en gang. Nå er den tilstandsavhengig og lik begge steder:
  har du **ulagrede endringer** → tilbake til sist lagrede utkast (angrer endringene); er du **ren**
  (ingen ulagrede endringer) → forkast hele utkastet → tilbake til det **aktive/live** kartet. Slik
  kommer du progressivt: ulagrede endringer → lagret utkast → aktiv. Logikken ligger nå ÉN plass i den
  delte kart-kjernen (`map_renderer.js`), og begge editorene kaller den samme funksjonen.

## 1.5.2 — code-review-fikser + ny tier-basert plasserings-motor («Nytt»)

Funn fra en uhildret code-review (`review-clauditself-22jun2026.md`). Batch 1:

- **Config (G-242):** `docker-compose` manglet `env_file: .env` → env-var som koden leser (COOKIE_SECURE,
  MAX_UPLOAD_MB, USER_MAX_DAYS, retensjons-intervaller, MAP_SOURCE\* m.fl.) nådde aldri containeren og ble
  stille ignorert. Lagt til `env_file: .env` på webapp + poller.
- **Sikkerhet (G-243):** NetBox-token fulgte med ved HTTP-redirect → kunne lekke til vilkårlig host. Følger
  ikke lenger redirects (`allow_redirects=False`).
- **Sikkerhet (G-244):** anonym infoscreen-kiosk rendret kart uten status-sjekk → et senere arkivert/
  avpublisert kart kunne lekke via en gammel slide-ref. Krever nå `status='published'` (som public-visningen).
- **Bug (G-245):** advisory-lås-nøkkel `919284` ble delt av OSPF-discovery og inventory-rescan → unødig
  blokkering. OSPF-discovery bruker nå distinkt nøkkel.
- **Bug (G-249):** SNMP-engine/UDP-socket ble aldri lukket i LSP-collectoren (fd-lekkasje). Lukkes nå.
- **Ytelse (G-250):** `_swap_optimize` fikk node-grense (≤70) så store topologi-kart ikke overbelaster
  request-tråden.
- **Opprydding (G-251):** fjernet død kode (ubrukte funksjoner `run_improve_layout`/`is_running`/
  `_relayout_members`, konstantene `usable_gps`/`INTERNAL_ROLES`, død fil `db/config_schema.sql`).
- **Config-doc (G-252):** dokumenterte tidligere udokumenterte env (MIN_PASSWORD_LEN, LSP_REBUILD_INTERVAL,
  SNMP_TIMEOUT/RETRIES) i `.env.example`; rettet feilmerket `rescan_hours`-label (tidsvindu, ikke intervall).
- **Sikkerhet (G-248):** eksisterende-link-menyen (slett/bytt) på fysisk kart krever nå redigeringsmodus
  (var ikke gated → kunne slette en link med Rediger av). Som node-menyen.
- **Opprydding (G-253):** fjernet dødt dobbelt status-element på fysisk kart (partialens `#eb-edit-status`
  brukes kun av logisk; fysisk har sitt eget `#save-status`).
- **Bug (G-254):** tre-tilstands interface-status i poller. Før ble et interface satt `oper_up=true` så
  snart noe ble lest — også når SNMP-status manglet (mangel på data ble vist som «oppe»), og host-
  tilgjengelighet ble talt på rader (et host som svarte uten tellere ble feilaktig regnet som nede). Nå:
  trafikk → oppe; null trafikk + `ifOperStatus` nede → nede; null trafikk + ukjent status → urørt
  (manglende data, ikke «oppe»); tilgjengelighet telles på om hosten faktisk SVARTE. Rettet også
  retnings-normalisering i `aggregate_by_site` (Stage-3-backend, parkert G-218): inter-site-rater
  summerte A-endens in/out uten å normalisere mot lenkeretningen → blandet inn/ut når lenker pekte hver
  sin vei. Nå konsistent lo→hi («out») / hi→lo («in»).
- **Kart (G-241):** GPS er nå en **ren ikke-låsende start-posisjon**. Tidligere var GPS halvt en lås og
  halvt en egen modus: når minst én cluster hadde GPS hoppet motoren over meta-FR + skalering, og GPS-
  noder ble låst i kryssings-reduksjonen — så GPS-løse enheter kunne bli liggende dårlig plassert. Nå
  kjører alle pass likt; GPS gir kun startposisjonen, og de senere stegene står fritt til å flytte for
  et bedre helhetsbilde. Eneste lås er manuell «lås posisjon» (pinning). I tillegg: velger man «GPS
  eksakt» uten et geo-referert bakgrunnskart, faller plasseringen automatisk tilbake til «GPS relativ»
  (spre relativt utover) i stedet for å gi feilmelding.
- **Ny plasserings-motor på «Nytt» (G-256):** «Nytt»-knappen bygger nå kart med en helt ny, **tier-basert
  inkrementell** motor. Den tolker enhetenes rolle fra navnet (en tunbar tier-tabell: core/re=ryggrad …
  sw/edge=løv), sprer ryggraden vidt utover for å **bruke mest mulig av kartet**, nester resten («juletrær»)
  i mellomrommene, kutter kryssende linker, orienterer mot geografien, og **garanterer at ingen node ligger
  oppå en lenke**. «Auto» beholder den gamle, beviste motoren uendret — så du har **to motorer å velge mellom**.
  «Forbedre» foredler nå dagens plassering på begge (færre kryssinger, bedre plass-bruk, dytter noder av
  lenker) og **respekterer låste noder** som faste ankre. Ny motor brukes på kart ≤30 noder uten manuell
  låsing (der den skinner og er rask); større/låste kart bruker den raske gamle motoren.
  (Fart-optimalisering for store kart = videre arbeid.)
- **Bug (G-247):** GPS-utgangspunkt-dropdownen (av/relativt/eksakt) ble **ignorert på logisk kart** — den
  logiske relayout-veien (`compute_logical_layout`) tok aldri imot GPS-modusen fra editoren, så valget
  gjorde ingenting der (i motsetning til fysisk kart). Nå sendes modusen gjennom begge logiske relayout-
  ruter, og GPS-seeden droppes når man velger «av» — som på fysisk kart.
- **Korrekthet (G-246):** per-element edit-ruter (auto-layout, place-gps, smart-layout, opprett/slett link,
  fjern node, etiketter, bytt enhet, bakgrunn) manglet status-vakten `save_map` har → kunne (ved direkte/
  stale kall, også som admin) mutere et **publisert** kart direkte i stedet for arbeidskopien. Nå avviser
  en sentral vakt (`_maps_guard`) innholds-edit mot publiserte kart med 409 — rediger arbeidskopien.
- **Opprydding (G-251):** fjernet ubrukt `OUR_ASN`-modul-konstant (settings «our_asn» leser env direkte) og
  siste døde JS-kode (identisk ternær i `map_physical.js`; write-only-variablene `leavingForReload`/
  `savedDeliberately` i `map_physical_edit.js`). G-251 komplett (W_HUB/HUB_GRAVITY_MULT brukes fortsatt av
  `layout.py` og er bevisst beholdt).
- **Config (G-252):** `RESCAN_INTERVAL` (ifAlias-synk-intervall) lagt til i innstillings-registeret (var
  udokumentert poller-env).
- **Sikkerhet (G-255):** SNMPv3 auth/priv-passordfelt på admin-siden er nå `type="password"` (var klartekst
  i skjemaet); `requests.Session` i geo-snapshot lukkes nå (frigjør socket-pool); **utlogging er nå POST +
  CSRF-beskyttet** (var GET — kunne CSRF-utlogges via `<img src=/logout>`); og gamle Argon2-passordhasher
  **oppgraderes transparent** til gjeldende parametre ved neste vellykkede innlogging.

## 1.5.1 — bugfiks: fysisk kart faller ut av redigeringsmodus ved link-/node-endringer (G-240)

- **Bli i redigeringsmodus når du endrer en link eller node.** På fysisk kart reloades editor-siden
  når du oppretter/sletter/redigerer en link eller sletter/bytter en node — og man falt da ut av
  redigeringsmodus. Nå bevares redigeringsmodus over slike reloads (samme «bli i editor»-mekanisme som
  «Forkast»). «Aktiver» går fortsatt til visningen som før.

## 1.5.0 — plasserings-motor: NetBox + GPS-plassering, kryssings-minimering, universelt GPS-innsyn

Den store plasserings-motoren — «hjernen i Bifrost»: plasser nettet automatisk, oversiktlig og med
færrest mulig kryssende lenker, felles for fysisk og logisk kart. NetBox + GPS gir et geografisk
startpunkt; algoritmen bygger videre. (Site/zoom-nivå = en senere runde.)

- **Smartere plassering: gruppering + kryssings-minimering (G-238).** Motoren tolker nå hostnavn
  (lokasjon vs. funksjon — `bergen-aggr-sw01` ⇒ Bergen + aggregerings-switch) og bruker **konnektivitet**
  som samvær-signal (mange switcher på samme ruter havner sammen), i tillegg til NetBox-site. Den
  minimerer **kryssende lenker** (barycenter-sortering mellom nivåer + retnings-sortering av ringer) og
  måler resultatet med en kvalitetsscore. Mål: ett ryddig bilde der ting står fornuftig i forhold til
  hverandre. Felles motor → slår inn på både fysisk og logisk kart.
- **GPS er et ikke-låsende startpunkt (G-207).** GPS plasserer enhetene som *har* koordinater (eksakt på
  geo-bakgrunn, eller relativt til hverandre uten) som et **utgangspunkt** — de senere stegene står fritt
  til å flytte for en bedre helhet. GPS/site kommer kun fra NetBox (G-209).
- **«📍 GPS / NetBox-info» på alle kart (G-235/G-237).** Node-menyen viser nå datakilde (NetBox vs.
  manuell), site, GPS-koordinat, om geo-bakgrunn finnes og nøyaktig kart-posisjon — med en **«GPS
  plasser»**-knapp som flytter noden til GPS-posisjonen. Universelt på fysisk og logisk kart fra én kilde.
- **NetBox-innstillinger samlet (G-236).** URL + token + TLS-verifisering settes i **én** form som lagrer
  alltid (en tilkoblingsfeil gir kun en melding, ikke avvisning), verifiserer tokenet, og er beskyttet med
  en forsøks-grense. Hemmeligheter vises maskert (første 4 + siste 4 tegn). «Hent posisjoner fra NetBox»
  gir en **per-host-tabell** som viser nøyaktig hvilke enheter som fikk site/GPS og hvilke som ikke ble
  funnet.
- **Tre tydelige modi av autoplassering + GPS som toggle (G-227).** Knappene Auto/Forbedre/Nytt er nå
  tre modi av den ene motoren: **Auto** = bygg fra bunnen med fast seed (forutsigbar — samme resultat
  hver gang); **Forbedre** = foredle *dagens* plassering (færre kryssende lenker, bedre plassutnyttelse) —
  bygger ikke på nytt; **Prøv på nytt** = bygg fra bunnen, men annerledes. **GPS** er nå en *toggle*
  (av / relativt / eksakt) som virker som geografisk utgangspunkt KUN når kartet bygges fra bunnen
  (Auto/Nytt), aldri for Forbedre — de to gamle GPS-knappene er slått sammen til én velger.
- **Forutsigbar exit fra redigeringsmodus — arbeidet bevares (G-232).** Tidligere forkastet fysisk
  kart arbeidskopien hvis du navigerte vekk (mistet arbeid). Nå **bevares** arbeidet ved exit/navigering
  på alle kart, og du **gjenopptar** der du slapp når du går tilbake i redigeringsmodus. Forkasting
  skjer kun via eksplisitt «Forkast utkast». Likt på fysisk og logisk kart.
- **Enhetlig editor-knapperad på ALLE kart (G-233/G-168).** Fysisk og logisk kart deler nå nøyaktig
  samme redigerings-rad fra ÉN kilde (`_map_editbar.html`): Auto/Forbedre/Nytt · Lagre utkast/Forkast/
  Aktiver · Bakgrunnskart · Rutenett + GPS av/på · Rediger. Knappene wires felles i kart-kjernen, så en
  endring gjelder alle karttyper (nå og framtidige). Karttype-spesifikke knapper ligger over den faste
  raden (fysisk: Forhåndsvis m.fl.; logisk: metric/flyt). Eneste reelle avvik er by design: linker
  (fysisk lagres / logisk genereres) og hvor «Aktiver» sender deg (fysisk → presentasjonsside).
- **Auto/Forbedre/Nytt er nå ÉN felles funksjon for alle kart (G-233, fullfører G-168).** Plasserings-
  knappene var wiret hver for seg i fysisk og logisk editor; nå kaller begge samme `runAutoLayout` i
  kart-kjernen. Logiske OSPF-kart oppfører seg dermed identisk med fysiske: plasser uten å lagre/forlate
  editor, ingen advarsel (angre = forkast utkast). Universelle redigerings-funksjoner bor ett sted —
  en endring gjelder alle karttyper (viktig når flere karttyper kommer).
- **Auto/Forbedre/Nytt plasserer uten å lagre eller forlate editoren (G-227).** Knappene flytter nå
  bare enhetene på arbeidskopien så du kan justere videre — de **auto-lagrer ikke**, **lukker ikke**
  redigeringsmodus, og advarselen er fjernet. Server beregner og returnerer posisjoner (skriver ikke);
  editoren tegner dem på nytt. Angre = «Forkast utkast». «Lagre utkast» beholder.
- **Plasserings-forrang + ghost-geografi på logiske kart (G-231/G-230).** Hver node behandles likt
  (navn + IP) — men **kjente** routere (vi monitorerer dem) plasseres autoritativt først, og **ghost**-
  routere (OSPF-naboer uten egen host hos oss) settler i en ring rundt sine kjente naboer uten å dytte
  dem ut av posisjon. I tillegg kan ghost-routere få **site + GPS fra NetBox** (slått opp via loopback-IP,
  cachet på `ospf.node`) — da plasseres de geografisk som kjente noder. «Hent posisjoner fra NetBox»
  henter nå både hoster og ghost-routere.
- **Samme plassering på ALLE kart, også logisk (G-221).** Logiske OSPF-kart mater nå den ene motoren
  med **site + rolle + GPS** for hver router (koblet via `ospf.node` → host), akkurat som fysisk kart —
  så GPS-utgangspunkt + algoritme + de tre modusene gjelder uansett karttype. Ghost-routere (uten kjent
  host) plasseres rent topologisk. OSPF eier fortsatt link-/objektutvelgelse; motoren gjør bare plassering.
- **ÉN sentral plasserings-algoritme (G-220).** Tidligere fantes to motorer: gammel `layout.py`
  (force-directed, brukt av alle kart) og ny `placement.py` (4-pass, brukt kun av test-panelet).
  De er nå **slått sammen** til `placement.smart_layout` — den ENE motoren: 4-pass-struktur (INIT-63)
  + GPS-seed + de velprøvde bygge­klossene fra den gamle (grid kollisjonsfri celle-tildeling, fyll-
  lerretet-skalering, kantlengde-optimering, kvalitetsmål). `layout.py` er nå et tynt skall som
  delegerer dit, så all kart-plassering går gjennom **samme** algoritme — en justering påvirker alle
  kart. (Caller-berikelse for full clustering på alle karttyper er flagget som G-221.)
- **Sentral 4-pass-motor — kvalitet + pinning (G-201/G-216).** `placement.py`: garantert
  **overlapp-fritt** resultat (grid-snap før overlap-resolve, in-bounds + deterministisk dytting).
  **Låste (pinned) noder er nå absolutte ankre:** de flyttes aldri, og alt som kjøres etter GPS-seed
  tar hensyn til dem — bevegelige noder ryddes helt klar av låste, og medlemmer av en site med låste
  noder arrangeres *rundt* der låsen faktisk står. Demo-seedet får `site` pr. host så clustering virker
  i mock.

- **NetBox-integrasjon (G-201).** Ny `webapp/netbox.py` + to innstillinger (**NetBox URL** + **token**)
  på Admin → Innstillinger (samme mønster som Google-nøkkelen; validert med ekte NetBox-oppslag ved
  lagring). «Hent posisjoner fra NetBox»-knapp på Admin fyller **site + GPS-koordinat pr. host**
  (matchet på hostnavn, ev. loopback-IP). Idempotent — fyller, sletter aldri (COALESCE).
- **Geografisk plassering på kart (to varianter).** I kart-editoren: **«Plasser relativt (GPS)»**
  (sentroide → lerret-senter, skaler til å passe, bevarer nord/sør/øst/vest — ingen bakgrunn nødvendig)
  og **«Plasser eksakt på kartet»** (nøyaktig der GPS treffer et geo-referert bakgrunnskart). Enheter på
  samme site klynges i en ring (ikke oppå hverandre). Respekterer manuelt låste noder; hopper over noder
  uten GPS. Plasseringen er et **seed** (pinner ikke), så senere stadier/auto-layout kan finpusse.
- Gjenbruker eksisterende `host.site/lat/lon`-kolonner + `geo.py` Web Mercator-projeksjon. Ingen DB-
  migrering, ingen poller-endring (NetBox = HTTP, ikke underlagt SNMP-cooldown).
- **Demo-seedet får GPS-koordinater (G-219).** De 9 demo-hostene har nå realistiske WGS84-koordinater
  (de fem core-ene = sine byer Oslo/Bergen/Trondheim/Stavanger/Tromsø; dist-* forskjøvet mot sin
  himmelretning nær uplink-byen — Alta/Kristiansand/Hamar/Haugesund). Gir `seed-demo` et realistisk
  geografisk grunnlag for å teste GPS-plassering i mock (uten ekte NetBox). Orientering verifisert
  geografisk korrekt (nord opp/øst høyre) — en tidligere antatt «speilvending» viste seg å skyldes
  manglende GPS-data, ikke en faktisk flip.

*NB (automode-bygg 2026-06-19): flere antakelser er flagget som G-saker (G-202…) for gjennomgang —
NetBox-endepunkter/matching/site-GPS-felt kan trenge justering mot ekte NetBox.*

## 1.4.14 — bugfikser fra felttest av 1.4.13

- **G-193 — infoscreen-lenken bruker nå riktig URL bak en reverse-proxy / sti-prefiks.** Admin viste
  før lenken som `request.host_url + "screen/…"`, som droppet et eventuelt sti-prefiks (Bifrost kan
  ligge bak `/bifrost/`, `/a/` osv.). Bygges nå med `url_for(..., _external=True)`, som tar med host,
  scheme og prefiks fra X-Forwarded-headerne (ProxyFix) — så token-lenken stemmer uansett hvor appen er montert.

## 1.4.13 — infoscreen / NOC-kiosk (G-193)

- **G-193 (del 1–2) — infoscreen-administrasjon.** Ny «Infoscreens»-side (admin): opprett flere
  navngitte NOC-/infoskjermer, hver med en unik, ugjettbar **12-tegns token-lenke som vises uten
  innlogging**. Pr. infoscreen legger du til **bilder** (slides) i ønsket rekkefølge — et **fysisk
  kart** eller en **kategori-graf** (Internett/Backbone/Fordeling) — med egen **tekst (caption)** og
  **visetid**. Token kan regenereres (tilbakekalling av lenke). Generisk datamodell
  (`composer.infoscreen` + `infoscreen_slide`) klar for flere slide-typer (logiske kart senere).
- **G-193 (del 3a) — kiosk-visningen.** `/screen/<token>` viser nå infoscreen-en **uten innlogging**:
  et chrome-løst skall som **roterer** mellom bildene (hvert med sin visetid, evig loop) og
  **auto-refresher** hver runde. **Fysiske kart-slides** rendres med live trafikk-data, token-gated og
  whitelistet (en token når KUN sin egen infoscreens bilder — ugyldig token/posisjon → 404).
- **G-193 (del 3b) — graf-slides + sentral kategori-katalog.** Graf-bilder rendres nå for ekte
  (uPlot + charts.js, chrome-løst) med token-gated data. Du kan velge **hvilken som helst graf** fra
  **samme kategori-inndeling som bruker-tilganger** — Oversikt, Backbone, hver kategori (IX/PNI/CDN/
  transit/peering, NNI/core/distribution) og hver kunde. Den inndelingen er flyttet til **én sentral
  sannhetskilde** (`catalog.py`) som både bruker-grants og infoscreen bruker — og som er klar for den
  fremtidige bruker-/gruppe-endringen. (Fordeling/flow + OSPF-routing er bevisst ikke med.)

## 1.4.12 — bugfikser fra felttest av 1.4.11

- **G-192 — «Logiske kart»-siden lik den fysiske kart-siden.** Samme oppsett som fysiske kart:
  opprett-skjema med router-velger øverst (rutenett av avkrysningsbokser + «Velg alle», delt i to —
  **kjente hoster øverst**, tynn strek, **ukjente/ghost-routere** under), og en **kart-tabell** med
  navn / status / antall noder / opprettet og **samme knapper**: Åpne, Rediger, Endre navn, Publiser,
  Parker, Arkiver / Gjenopprett, og Slett (dobbel-bekreftelse). Livssyklus-rutene er scopet KUN til
  custom logiske kart — auto-area-kartene og fysiske kart kan aldri treffes.
- **G-192 — «Legg til tilgrensende ghost-noder»-knapp.** På et custom logisk kart kan du med ett klikk
  legge til alle **umonitorerte (ghost-)naboer** av routerne du allerede har valgt — de som binder
  kartet sammen, men som ellers ville manglet. Knappen viser antallet og forsvinner når det ikke er
  flere igjen. (Idempotent; du kan alltid fjerne enkeltnoder etterpå.)
- *(Tidligere i 1.4.12: ghost-routere ble sortert foran de kjente hostene i velgeren — nå rettet via
  grupperingen over.)*

## 1.4.11 — logisk kart: custom kart (utvalg routere) + OSPF-metric av/på + Navn/IP som én toggle

- **G-192 — custom logiske kart: velg hvilke routere et kart skal vise.** Du kan nå lage navngitte
  logiske kart av et utvalg routere (i stedet for bare auto-kartet pr. OSPF-area med alle). Som et
  fysisk kart, bare at lenkene beregnes fra OSPF i stedet for å bygges manuelt:
  - **Opprett + liste** på «Logiske kart» — velg routere, gi kartet et navn.
  - **Rediger** som de andre kartene: dra/plasser routerne, lagre utkast/aktiver, auto-arrangér,
    velg bakgrunn — pluss legg til / fjern routere på kartet.
  - **Areaer, lenker, ghosts og cost faller ut** av de valgte routerne (felles topologi-database);
    bare 0.0.0.0 → ett kart, flere areaer → multi-area-presentasjonen slår inn av seg selv.
  - **Kan eksponeres public** som auto-kartene.
  - Lagret pr. kart: hvilke routere + plassering + bakgrunn; resten beregnes.
  - Teknisk: render- og editor-regelen er nå **felles for alle logiske kart** (auto OG custom) —
    endres ett sted. Auto-area-kartene er uendret (regresjons-verifisert: identisk resultat).

- **G-200 — OSPF-metric (cost-merking) kan slås av/på på det logiske kartet.** Ny toggle-knapp
  «Skjul/Vis metric» skjuler eller viser cost-chipsene på lenkene (klient-side, ingen reload) — et
  første grep for å vise mindre på en travel skjerm. Default på. Cost vises fortsatt på hover selv når
  chipsene er skjult.
- **Navn/IP slått sammen til ÉN toggle-knapp.** De to knappene «Navn» / «IP» er nå én knapp som viser
  gjeldende modus og bytter ved klikk (Navn ⇄ IP), likt «Skjul ukjente»/«Skjul områder». Frigjør plass
  i kontroll-linja. Kun det logiske kartet.

## 1.4.10 — opprydding: fjern utdatert kart-kode

- **G-188 — slettet de gamle, ubrukte kart-skriptene `map_view.js` og `map_edit.js`.** De ble erstattet
  av den delte kart-kjernen (`map_renderer.js` + `map_physical.js`/`map_physical_edit.js`) i 1.4.7 (G-184)
  og har siden ligget DEPRECATED uten å lastes noe sted. Nå felt-verifisert at den nye motoren fungerer i
  prod → de gamle filene fjernes. Ingen funksjonell endring; ryddet samtidig vekk de utdaterte «beholdt,
  merket DEPRECATED»-kommentarene i `map_physical.js`, `map_physical_edit.js`, `bg_live.js` og kart-templatene.

## 1.4.9 — opprydding + småfikser

- **Kart-bygg sletter ALDRI på tomt/delvis resultat (data-tap-guard).** Et OSPF-/LSP-bygg rydder/erstatter
  nå KUN etter et **rent fullt sveip** — alle hoster svarte, ingen i cooldown, og vi faktisk fikk data. Et
  tomt eller delvis bygg (SNMP-feil, cooldown, 0 naboskap) **beholder eksisterende topologi/LSP-er** i
  stedet for å slette dem. Prinsipp: «hent feilet» ≠ «alt forsvant». For LSP erstattes kun rutene til
  routere som **faktisk svarte** — en host som ikke svarte beholder sine LSP-er. (Gjelder kun de
  gjenoppbyggbare `ospf.*`-tabellene; metrikker/hoster har aldri vært berørt av bygg.)
- **Synlig feil ved navne-kollisjon i kategori-mappingen.** Å døpe om en mapping-regel til et søkeord en
  ANNEN regel alt bruker ga før en stille 500 (UNIQUE-brudd). Nå vises en tydelig melding
  («… finnes allerede som en annen regel …») i stedet.

## 1.4.8 — bugfikser/polish fra felttest av 1.4.7

- **G-198 — beregnet LSP-rute (primær + node+link-disjunkt backup) i «Velg to noder».** Når OSPF+LSP
  viser en LSP mellom to valgte noder, tegnes den langs en **beregnet rute**: **primær** = OSPF korteste
  vei (Dijkstra). **Backup** (hvis sekundær-LSP finnes) = en **fullt disjunkt alternativ** — deler verken
  link eller router (utstyr) med primær, så den overlever enhver enkeltfeil på primær; finnes flere
  disjunkte alternativ vinner laveste IGP-metric. (Deler kun noe hvis topologien tvinger det.) Statusen er
  **ærlig på at det er beregnet**: «beregnet rute (antatt OSPF/IGP)». **Null ekstra SNMP** — ren beregning
  på OSPF-topologien vi alt har (gjenbruker forwarding-motoren fra G-186), trygt på svakt gear.
  **Visuelt:** primær = lilla «elve»-kurve (glatt Catmull, forbi/utenfor nodene, sving inn mot bildets
  midte), backup = **teal** elve-kurve (egen farge); OSPF-sti = rett lilla linje til node-senter. **«LSP
  fra node»** = kun **working**-LSP-er som rette lilla linjer (eksistens + retning; backup vises kun i
  «Velg to noder»). (Visuell finjustering forventes etter fullskala felttest.) Begrensning: en eksplisitt/
  TE-constrained LSP kan avvike fra den beregnede ruten — den *faktiske* ruten (definert vs. IGP-løs)
  hentes evt. senere via NETCONF (G-199, parkert).
- **Logg kart-bygg-FORESPØRSLER (ikke bare worker-start).** «Rebygg OSPF» / «Rebygg LSP» logger nå at
  en admin **ba om** bygget (lagt i kø / alt i kø-kjører), så loggen viser hele kjeden forespørsel →
  worker-start → ferdig (tidligere så man bare worker-en starte jobben).

## 1.4.7 — felles kart-motor: fysisk + logisk på ÉN kjerne (G-168 fase 3) + public logisk kart

- **G-196 — global per-host SNMP-cooldown + bakgrunns-jobb for tunge bygg (kritisk SNMP-fiks).**
  ALLE on-demand-SNMP-kall i appen (rebygg OSPF, rebygg LSP, re-skann) går nå gjennom en **global,
  DB-basert per-host-cooldown** — samme router treffes aldri oftere enn cooldown'en, uansett kilde,
  bruker eller gunicorn-worker. Atomisk «claim» (`host.last_snmp_at` + `snmp_claim()`) hindrer at to
  samtidige operasjoner hamrer samme enhet. Ny innstilling **`snmp_cooldown_min`** (default 1 min,
  minimum 1). **Den planlagte polleren er en egen prosess og er UNNTATT** (hjertet skal alltid gå).
  «Rebygg OSPF» / «Rebygg LSP» legges nå i en **bakgrunns-jobb** (generalisert `rescan_job`-kø, ny
  `kind`) og kjøres av worker-tråden — et bygg kan ta opp mot ~75 s når cooldown-**defer** slår inn
  (treffer en host en sperre: vent 45 s, så 30 s, så gi opp), og det ville ellers sprengt request-
  timeouten. **Ingen data slettes** ved delvis bygg: gir en host opp pga cooldown, hoppes rydde-
  slettingen over (OSPF) / kun spurte routeres LSP-er erstattes — en utsatt host mister aldri topologi.
  OSPF-siden viser utsatte hoster i siste-kjøring-statusen.
- **G-197 — poll-intervallet låst til fast 5 min.** Hele systemet (grafer, lagring, rater) er bygd på
  eksakt 5-minutters poll → intervallet er nå deploy-låst til 300 s (vises skrivebeskyttet).
- **G-195 steg 3 + G-194 — LSP-fra-node-lense + ghost-bevisst routing (logisk kart).** «Trafikkflyt»-
  knappen er nå en **3-state lense-toggle** (som OSPF/OSPF+LSP): **Av** (klikk node = node-meny
  Adopter/Lås/Omdøp) → **«Velg to noder»** (routing A→B) → **«LSP fra node»** (klikk én node → alle
  LSP-ene som starter i den lyser opp, working heltrukken / backup stiplet). Uten LSP-data grås «LSP
  fra node» ut + viser advarsel (men kan toggles forbi). **G-194 — forwarding gjennom/til ghost-noder:**
  SPF ruter nå korrekt **til** en umonitorert (ghost) router (alt kjent — eksakt) og **gjennom** en
  ghost-transit-node når det faktisk er korteste vei. Ghost-ens ukjente utgående metric **estimeres
  symmetrisk** (= linkspeed-basert auto-cost i ~98% av tilfellene, jf. [[G-176]]) og **flagges som
  estimat**: stiplet sti-segment, stiplet varsel-ring på ghost-noden, og status-note «via X
  (umonitorert) — utgående metric estimert». Ghosts på den valgte stien auto-vises selv med «Skjul
  ukjente» på, så stien aldri «forsvinner». Vi ruter aldri rundt via en KJENT dyrere sti for å unngå
  en ghost.
- **G-191 — MPLS-LSP-er på logisk kart (OSPF+LSP-routing).** Routing-lensa fra [[G-186]] får LSP-laget:
  **velg én node** → se **alle LSP-ene fra** den (aktiv + backup); **velg to noder** → routing med toggle
  **«OSPF+LSP»** (faktisk routing — LSP vinner der den finnes, ellers OSPF) eller **«OSPF»** (kun IGP, til
  sammenligning). LSP tegnes **teal** (working heltrukken / backup stiplet+dempet), OSPF-sti **fiolett**.
  «Velg noder»-knappen er én toggle (re-trykk = nullstill). **Collector:** best-effort MPLS-TE-MIB
  (`mplsTunnelName`-indeks → from/to router-id), gated av ny innstilling **`mpls_lsp`** (default av,
  **UTESTET mot ekte gear** som OSPFv3 — full ARHop-sti er en felt-verifiserings-utvidelse). **MOCK/demo**
  viser alltid eksempel-LSP-er (working+backup) så visualiseringen kan testes uten MPLS-gear.
- **G-186 (OSPF-laget) — node-drevet forwarding/trafikkflyt på logisk kart.** Ny **«Vis trafikkflyt»**-modus:
  velg node A → node B, og kartet lyser opp **OSPF-stien** mellom dem — korteste vei over **cost** (Dijkstra
  på de oppe-naboskapene, rettet: a→b vekt = a_metric, b→a = b_metric) — med pil i forwarding-retningen og
  uthevede endepunkter (A grønn, B blå). Status viser cost + antall hopp. Dette er «hvilken vei tar trafikken»,
  ikke trafikk-mengde. **LSP-laget (LSP > OSPF som to lag av sannhet) plugges på via G-191.** ECMP (vis alle
  like-kost-stier) er en senere forbedring; v1 viser én sti.
- **G-190 — felles redigerings-bar i bolker (begge editorer).** Kart-knappene struktureres i faste
  **seg-bolker**, lik struktur på **logisk** og **fysisk** editor: **Arranger** (Auto/Forbedre/Nytt) ·
  **Utkast** (Lagre utkast/Forkast/Aktiver) · **Bakgrunn** (Bakgrunnskart) · **Rutenett** (nå **dropdown**
  med valgt verdi vist) · til høyre **Rediger** (logisk) / **Forhåndsvis** (fysisk). Ingen overskrifter —
  grupperingen vises av seg-boksene. På logisk **grås bolkene ut** utenfor Rediger (synlige, men inaktive;
  stabil layout); fysisk editor er alltid i redigering → alltid aktiv. Logisk auto-arranger fikk **3
  moduser** (seed redo/improve/nytt) som fysisk. Status flyttet til egen rad (ingen wrap av baren). Fysisk
  visning + public uberørt. Bygd som gjenbrukbart mønster i kart-kjernen → neste karttype arver det.
- **G-184 — de to kart-motorene slått sammen til ÉN (G-168 fase 3).** Hele det fysiske kartet — både
  **visning** (innlogget `/maps/<id>` + public `/public/map/<id>`) og **editor** (`/maps/<id>/edit`) —
  rendres nå via den **delte kart-kjernen** (`map_renderer.js`) + per-type adaptere: `map_physical.js`
  (visning), `map_physical_edit.js` (editor) og `map_logical.js` (OSPF). Gamle `map_view.js` +
  `map_edit.js` er merket **DEPRECATED** (beholdt, ikke lenger lastet, fjernes i egen senere
  slette-release). Motoren eier nå **linje-geometrien** (`ctx.linkGeom`); hver ENDE fyller sin halvdel
  uavhengig (data eller outage). Editoren beholder sin oppførsel (flytt-uten-lås G-034, picker,
  enhetsbytte, oppløsning, snap-rutenett, publish/scratch-livssyklus) via kjernens nye `pinOnDrag`- +
  `onNodeClick`-kroker. **Ett tegne-/redigeringssystem for begge karttyper — releasens kjerne.**
- **G-187 — «halve linker» på fysisk kart fikset (G-175-regresjon).** En fysisk link slås nå sammen
  fra sine to interface-rader til ÉN render-lenke med to ender → linja tegnes alltid som én helhet med
  begge halvdeler. Er bare den ene siden mappet (foreldreløs) måles begge retninger fra den ene raden
  (som eksterne linker). Ingen halve linker lenger.
- **G-180 — synlig rolle + status på logiske noder.** Hver OSPF-ruter får nå en **status-prikk**
  (grønn = alle naboskap oppe / amber = noen ikke / rød = ingen / grå = ghost) + en **rolle-tag**
  (ABR / umonitorert) på selve noden — ikke bare i hover. Raskt helse- og rolle-blikk pr. ruter.
- **G-170 — node-antall-skalering av det fysiske kart-kanvaset.** Auto-layout vokser nå kanvaset
  (grow-only) etter antall noder, som det logiske kartet — så et stort nett ikke presses inn i fast
  1200×700. Små nett er uendret; pinnede posisjoner bevares (kanvaset bare vokser).
- **G-168 fase 2 — live-fliser-bakgrunn i kart-kjernen.** Kjernen (`map_renderer.js`) rendrer nå også
  **live kart-fliser** (Kartverket/OSM/Carto/Google) som bakgrunn — ikke bare lagret snapshot — via den
  delte `bg_live.js`. `area_view` leverer bg_live-info. Bakgrunns-modulen er dermed komplett i kjernen
  (snapshot + live + opacity) for begge karttyper.

- **G-173 (fase a) — trafikkflyt-overlegg på fysisk kart.** Ny **«Trafikkflyt»**-toggle viser de **3–5
  største flytene** som **bølgende piler gjennom kartet** — ikke pr. lenke, ikke oppå lenkene. Hver flyt er
  en ende-til-ende-sti (A → forbi B → mot D → mot E) beregnet ved grådig kjeding av dominant trafikk-retning,
  tegnet som en glatt spline som svinger NÆR (ikke gjennom) nodene og ender før siste node, med tykkelse ∝
  trafikkrate. Reberegnes ved hver oppdatering fra de siste ratene. Visuell *vurdering* fra interface-rater
  (ikke NetFlow). **Antall stier er konfigurerbart** på Innstillinger («Trafikkflyt: antall stier», 2–6,
  default 3) — poenget er å highlighte de største flytene, ikke alt; endringen slår inn neste gang flyt
  slås på. **Kun fysisk kart** — logisk trafikkflyt droppet (G-185 lukket: interne OSPF-link måles
  ikke); det egentlige logiske ønsket («hvilken vei tar trafikken») er forwarding-retning, eget punkt G-186.

- **G-183 — public-versjon av det logiske kartet.** Public-mekanismen (`/public/map/<id>`) er ikke lenger
  fysisk-bare: den **dispatcher etter karttype** (`composer.map.kind`), så et logisk OSPF-kart kan eksponeres
  på en egen, frittstående public-side (NOC-skjerm) — samme «public»-grant som fysiske kart. Public-siden
  skalerer hele kartet til skjermen (mobil → 100" NOC). Opphever det tidligere «nei til public for logisk».

## 1.4.6 — felles kart-motor + OSPF-forwarding (konsolidert i 1.4.7)

Arbeidet som er tagget 1.4.6 i git (G-184 sammenslåtte kart-motorer, G-186 OSPF-forwarding,
G-190 felles redigerings-bar, G-191 LSP-collector, G-173/G-180/G-189) ble sluppet samlet under
**1.4.7**-seksjonen over — se den for detaljer. (Ingen egen 1.4.6-release; ført opp her så
versjonsrekka er hel.)

## 1.4.5 — kart-optimalisering (farger + ABR-synlighet)

- **G-179 (fargedelen) — fargekodede OSPF-areaer + tydeligere ABR-er.** Hvert area får en distinkt, stabil
  farge; expand-boblene fylles i area-fargen (multi-area ABR = flere fargede bobler), dropdown/area-meny
  får matchende farge-swatch, og ABR-er («grense-routere») gjøres tydeligere. Fargen er area-ets identitet
  konsistent overalt, så samme farge = samme area binder kartet sammen uavhengig av posisjon.
- **Logisk kart: nabo-områder som ring rundt ABR + ruter-info på hover.** Inline expand-in-place er fjernet
  (ble rotete på komplekse nett). I stedet ligger nabo-områdene som fargede bobler i en **tett ring rundt
  grense-routeren** (de «henger på» den); **klikk hopper** til det området. **Hover på en ruter** viser nå
  ROLLE (ABR / intern / umonitorert) + hvilke områder den er i + **ekte OSPF-status** (naboskap oppe / ikke
  oppe). Ny toggle **«Skjul områder»** (ved siden av «Skjul ukjente») skjuler område-boblene.
  Boblene legges på routerens **frie side** (motsatt av lenkene, så de ikke dekker linker — best-effort;
  formaliseres i plasserings-motoren), og backbone vises som **«0.0.0.0»** med en sterk farge (ikke «core»).
- **G-181 — Lagre utkast / Forkast utkast på logisk kart (feature-parity m/ fysisk).** Logisk kart får nå
  samme grunnpilarer som det fysiske: **Lagre utkast** (lagrer arrangementet ditt som et utkast uten å
  aktivere — live urørt), **Forkast utkast** (tilbake til aktivt kart), og **Lagre** (= commit/aktiver,
  skriver til live og tømmer utkastet). Utkastet lagres pr. kart og lastes når admin redigerer videre;
  view-brukere ser alltid det aktive kartet. (Felles lagre/utkast-logikk ligger nå i den delte
  kart-kjernen `map_renderer.js`, klar til å deles med fysisk kart i G-168.)
- **Bakgrunn på logisk kart (engine-parity).** Kart-kjernen tegner nå et bakgrunns-lag (lagret
  snapshot-bilde, dempet + diffust) likt det fysiske kartet. Logisk kart fikk en **«Bakgrunnskart…»**-
  lenke som gjenbruker den eksisterende bakgrunns-siden (last opp bilde + sett opacity) — `composer.map`
  er felles. (Live-fliser tegnes i den samlede kjernen senere, G-168.)
- **G-175 (logisk) — OPP / 1-vei / NEDE pr. halvdel på OSPF-lenker.** Hver lenke deles i to halvdeler
  (én pr. node), og **hver halvdel farges av SIN endes naboskap-tilstand**: OK (`full` — grønn), nede
  (dempet rød, = ifOperDown), 1-vei/forming (amber), ukjent (grå, sjelden). **Asymmetri vises da nativt**
  — en lenke der A har full adjacency men B ikke, blir grønn mot A og amber/rød mot B (ingen
  spesialtilfelle). Ghost/uleste motpart dekkes av den kjente enden (`full` krever toveis → oppe).
  Link-hoveren navngir tilstanden pr. ende for ikke-OK.
- **G-175 (fysisk) — skill NEDE fra «ingen data» + per-halvdel-status.** Det fysiske kartet skiller nå
  **ifOperDown** (dempet rød + «NEDE» — mindre skrikende enn før) fra **«ingen ferske data»** (nøytral grå,
  fin-prikket + «ingen data»). Og: hver interne lenke tegnes nå som **to halvdeler der hver ende fyller sin
  egen halvdel** (status + trafikk fra det interfacet) — så **asymmetri vises nativt**: er én side
  `ifOperDown` mens andre sender trafikk, blir lenken NEDE mot den ene noden og trafikk-farget mot den andre.
  Samme per-halvdel-modell som logisk. (Motoren eier selve linja/buen; hver node fyller sin halvdel.) Etter å ha låst et bakgrunns-utsnitt ble du
  sendt til den fysiske kart-editoren (tom/feil side for et logisk kart). Bakgrunns-siden returnerer nå til
  riktig kart basert på type — `/logical/<area>` for logiske, fysisk editor for fysiske.
- **Snap-til-rutenett på logisk kart.** Redigeringsmodus får en **rutenett-velger** (Fritt / 25 / 50 / 100)
  som snapper noder til rutenettet og viser et rutenett-overlegg — kun under redigering, aldri i visning
  (som det fysiske kartet). Klient-side hjelp i den delte kart-kjernen.

## 1.4.4 — generisk kart-kjerne (G-168 fase 1)

- **G-168 (fase 1) — felles renderer-kjerne for kartene.** Det logiske OSPF-kartet tegnes nå via en
  delt, data-drevet kjerne (`map_renderer.js`) + en tynn logisk adapter (`map_logical.js`), i stedet
  for én monolittisk fil. Kjernen eier alt felles (SVG/bakgrunn, dra/pinn, node-meny, tooltip, tema,
  kreditt, tegne-loop, generiske kontroller); adapteren leverer kun det type-spesifikke (node-form,
  cost-chips, ghost/ABR, expand-in-place). Grunnlaget for å samle fysisk + logisk kart bak samme
  kjerne i senere faser. **Ingen funksjonsendring for brukeren** — kartet ser likt ut.
- **G-177 — fikset latent krasj i expand-in-place.** Å folde ut et nabo-area inline kunne kaste
  (`shortName` var en dinglende referanse etter vrappe-refaktoren i 1.4.1/1.4.2) → tomt kart. Rettet
  (delgrafens bredde regnes nå som node-bredden ellers, `wrapLabel`-basert).
- **Expand-in-place-plassering.** Utfoldet nabo-area tegnes nå som en kompakt, nedskalert boks som
  klampes innenfor kanvaset (forskjøvet fra ABR-noden), i stedet for å blåses opp langt utenfor fokus.
- **G-178 — area-menyen skalerer (hybrid).** «core (backbone)» har en **▾**-nedtrekksmeny over de andre
  OSPF-area-ene; inline-knapper vises opptil 4 areas, over det core + gjeldende area + dropdown. Skalerer
  til mange areas uten at knappe-raden flyter over.

## 1.4.3 — siste polish før kart-rebuilden (G-168)

- **G-159 — trend-graf av trafikk-toppene.** Trafikk-topper-siden (`/topper`) får en **«Graf | Tabell»**-
  velger (egen bolk ved siden av Retning + Interval, som ASN/Funksjon på Fordeling; default Tabell).
  «Graf» tegner de daglige toppene (`peak_daily`) over tid som en uPlot-trendgraf, per serie, med samme
  retning/interval og tilgangs-scoping som tabellen — så man ser om toppene *vokser*.
- **Quickfix (G-159):** trend-grafene deles inn i SAMME seksjoner som tabellen (Internett/Backbone/
  Kunder) med **én graf pr. serie** under hver — alt i én graf ble uleselig.
- **Quickfix (logisk kart):** «tilstand»-linja fjernet fra link-hoveren — overflødig tekst siden
  lenke-fargen (full=grønn / ellers stiplet rød) viser tilstanden.

- **G-176 — skill manuelt satt OSPF-cost fra auto-beregnet.** På det logiske kartet markeres nå om en
  lenkes cost er **manuelt satt** eller **auto-beregnet** (ref-bw ÷ link-hastighet). Ny innstilling
  **OSPF referanse-båndbredde (Gbps)** på Innstillinger (default 1000) styrer auto-cost-beregningen.
  Discovery henter interface-hastighet (`ifHighSpeed` + `ipAddrTable`) for å avgjøre det; lagres pr.
  ende i `ospf.adjacency`. Vises i cost-hoveren + diskré markør på manuelt satte.

## 1.4.2 — logisk kart: polish + fikser

- **Ghost-adopt prefyller nå SNMP-community** med default-en («public», eller det som er satt på
  Innstillinger — G-167). Tidligere ble bare IP-en forhåndsutfylt, så community-feltet sto tomt ved
  adopt. (+ fikset en NameError som ga 500 på /admin.) Default-community er konfigurerbar på
  Innstillinger og kan overstyres per device.
- **G-174 — OSPFv3-collector (UTESTET).** OSPFV3-MIB-collector lagt til som drop-in, styrt av ny
  innstilling **«OSPF-versjon»** (`ospf_proto`: v2/v3/both, default v2). v3-koden er inert til du
  velger v3/both — null påvirkning på den verifiserte v2-stien. proto merkes pr. naboskap
  (v2/v3/v2+v3). **Ikke testet mot ekte v3-gear** (metric + per-nabo-area forenklet — justeres når
  v3-utstyr finnes).
- **Plassering bevares ved hvert bygg (inkrementell reconcile).** Et nytt OSPF-bygg (manuelt eller
  daglig auto) flytter ALDRI routere du har plassert — ALLE eksisterende noder beholder posisjonen
  sin (ikke bare pinnede), og kun NYE routere/ghosts plasseres (kollisjonsfritt blant de eksisterende).
  «Auto-arranger» gjør fortsatt en full re-layout når du selv ber om det.

## 1.4.1 — logisk kart: polish + fikser

Finpuss og fikser oppå 1.4.0-logisk-kart-funksjonen (egen versjon så det er tydelig hvilken
release som kjører).

- **Én kombinert kart-visning** — Cost/Nabo-tilstand-lensene (overflødige) slått sammen: cost-tall
  vises alltid, og lenkene fargelegges alltid etter naboskap (full=grønn, ellers stiplet rød).
- **Metric-verdien gjort mye tydeligere** — cost-tallet lå før og druknet i bakgrunnen; nå en
  solid, kontrastrik «chip» (større, bold) som står klart frem mot enhver bakgrunn.
- **Adopter ghost fra node-menyen** — i redigeringsmodus hadde en ghost-node bare «Omdøp»/«Lås
  posisjon». Nå har ghosts også **«Adopter (legg til host)»** øverst i menyen → tar deg til
  oppdag-flyten med loopback-IP forhåndsutfylt (samme som klikk på ghost i visningsmodus).

## 1.4.0 — logisk kart / OSPF

Nytt arbeidsspor **«logisk kart»** (G-160) — et selv-byggende logisk (L3/OSPF) kart over routerne
vi alt har i databasen. OSPF definerer topologien; kartet tegner seg selv via den eksisterende
auto-layout-algoritmen. Full design: `spec-logisk-kart-1.4.0.md` (prosjektrot).

- **G-161 — OSPF-discovery + `ospf`-skjema (lag 1).** Spør hver router (OSPF-MIB v2) om router-id,
  naboer (router-id + tilstand), cost og area; parer naboens router-id mot loopback-IP-en vi alt har
  i `host.address` (kjent vs ghost). Lagres idempotent i `ospf.node` / `ospf.adjacency` / `ospf.run`
  (per-ende cost + nabo-tilstand + area + `proto` for v3-klar). `MOCK_OSPF`/`DEMO_MODE` gir syntetisk
  topologi (testbar uten ekte gear). Singleton via advisory-lås (kun én kartlegging om gangen).
- **G-162 — topologi → tegnet kart (lag 2, reconcile).** Ett `composer.map kind='logical'` pr. area
  (`area_id`-kolonne); noder i `ospf.map_node`. Idempotent merge: layout/pinning bevares, nye noder
  auto-plasseres, forsvunne fjernes. ABR faller naturlig i flere area-kart. De fysiske kartene
  (`kind='physical'`) er HELT urørt. `manage.py ospf-build` kjører discovery + reconcile.
- **G-163 — drift: auto + on-demand.** Webapp-workeren bygger kartet om jevnlig
  (`OSPF_REBUILD_INTERVAL`, default 24t; 0=av) + manuell **«Bygg kartet nå»**-knapp (admin).
  Singleton via advisory-lås — kun én kartlegging om gangen.
- **G-164 — visning + ghost-noder.** Egen side (`/logical`) med SVG-render og **lense-velger**
  (Cost pr. ende / Nabo-tilstand) i samme stil som Inn+Ut/Inn/Ut. Naboskap som ikke er `full`
  markeres. **Ghost-noder** (umonitorerte OSPF-naboer) tegnes nedtonet/stiplet — kartet blir et
  blindsone-verktøy. Admin kan klikke en ghost → **scan-og-adopter** (discovery med IP forhåndsutfylt).
- **G-165 — area-navigasjon (alle tre mekanismer).** Default-view = area `0.0.0.0` (core).
  (iii) area-meny på toppen, (ii) **expand-in-place**: klikk en boble ved en ABR → nabo-areaet
  foldes ut inline (delgraf translatert ut fra ABR-noden, ingen reload), (i) «↗ åpne» i den utfoldede
  headeren → eget area-kart. Logiske kart filtreres ut av den fysiske «Kart»-menyen.
- **G-166 — tilgang.** Ny grant-kategori «Logisk kart (OSPF)» (admin ser alt; view-brukere tildeles).
- **G-167 — default SNMP community.** Managed app_setting `snmp_community` (default «public»,
  konfigurerbar på Innstillinger; DB > `.env` SNMP_COMMUNITY > default). OSPF-collectoren bruker den
  når en host ikke har egen community satt — så ekte-nett-kartlegging virker rett ut av boksen.
- **Redigering på det logiske kartet (G-165).** «Rediger»-modus (admin): dra routere (dra = pinn,
  som på det fysiske kartet), lås/lås opp posisjon, omdøp visningsnavn, «Lagre», og «Auto-arranger»
  (nullstill pinninger + auto-layout på nytt). Pinnede noder rører reconcile aldri → plasseringen din
  overlever neste auto-bygg.
- **Lenke-rendering forbedret (mindre støy, bedre lesbarhet).** Nabo-tilstand-teksten er fjernet
  fra kartet (opp/ned vises via farge + stiplet, detaljer i tooltip). Cost-tallene tegnes på en
  avrundet «chip» så de er lesbare mot enhver bakgrunn. Hver lenke deles i to med pil inn mot node
  (som det fysiske kartet), og cost-en for hver retning ligger på riktig halvdel ift. pilen
  (a→b = `a_metric`, b→a = `b_metric`) — asymmetrisk metric er lett å se.
- **Bygg logges** (start + ferdig med routere/ghost/naboskap-tall) til stdout + «Logg»-siden,
  for både manuell «Bygg kartet nå», auto-jobb og `manage.py ospf-build`.
- **Kreditt-stempel** nederst til høyre, som det fysiske kartet (G-155/158): «Generert av Bifrost ·
  <tid>» i visning, «Redigert med Bifrost · <tid>» i redigeringsmodus.
- **Fiks: dra-flytting** av noder fungerte dårlig (noden flyttet seg bare litt) — `draw()` slettet
  node-elementet som holdt peker-capturen ved hver redraw. Nå ligger capturen på det persistente
  SVG-elementet og peker→koordinat går via `getScreenCTM()` (robust mot skalering).
- **Ghost-noder kan slås av/på** («Vis/Skjul ukjente») — skjuler umonitorerte naboer + lenkene til
  dem, mot støy.
- **Ryddigere plassering:** kanvaset skaleres til antall routere, og rutenett-plassering (160 px)
  garanterer at noder ikke havner oppå hverandre.
- **Node-etiketter:** lange navn **vrappes** over to linjer (i stedet for å kuttes). **Navn/IP-toggle**
  bytter mellom hostnavn og IP-adresse på nodene. **Hover** på en node viser IP + hostnavn + hvilket
  OSPF-area den er i (ABR markert).
- **Fiks: kartet spilte ut av rammen sideveis.** Det logiske kartets SVG hadde fast piksel-bredde
  (`width=data.width`) og rendret i full størrelse i en smalere ramme. Nå `width="100%"` + viewBox +
  `preserveAspectRatio` (som det fysiske `#map-svg`) → skalerer responsivt til rammen, ingen overflow.
- **G-169 — host-omdøping validert.** Å lagre en host med navn ELLER adresse/loopback som alt finnes
  på en annen aktiv host avvises nå med en pen feilmelding (i stedet for en 500 mot unik-indeksen).
  Hindrer bl.a. duplikate loopback-er som ville brutt OSPF-paringen. Ingen lagring skjer ved kollisjon.
- **Fiks: ekte-gear NUL-feil.** OSPF router-id/area fra ekte SNMP (pysnmp `IpAddress`) ga
  «string literal cannot contain NUL» — nå `.prettyPrint()` (dotted-quad) + NUL-strip ved innsetting.

Verifisert i test-bifrost (DEMO/MOCK_OSPF): 9 hoster → 11 routere (2 ghosts) / 13 naboskap / 2 area-kart,
asymmetrisk cost + nabo-tilstand fanget, idempotent re-bygg, auto-jobb bygger når forfalt/hopper når fersk,
fysiske kart urørt. **Rogers felttest i nettleser gjenstår** (SVG-visuals, lense-toggle, ABR-dører,
ghost-adopter) + mot ekte gear (OSPF-MIB-stien — spec §15).

### Note — the changelog starts here, on purpose

Releases before 1.4.0 are not in this file. They cover Bifrost's first weeks — the first poller and
graphs, logins, the first maps, and the first security and robustness work — and they were written as
working notes: in Norwegian, with case numbers from a tracker nobody outside can reach. Most of it is
noise to someone reading Bifrost today. The untouched file is archived with the old repository.

This release is where the logical maps and the shared map engine begin, and with them Bifrost as it is
now.
