# Bifrost

Bifrost is traffic monitoring for edge, peering and transit networks that you run in-house. It reads
the interface counters on your routers over SNMP every five minutes, stores them, and shows where the
traffic goes: as graphs over time, as a picture of who you exchange traffic with, and as live maps of
the network.

The idea is to have one place for the everyday questions, with no dependency on any online service:
how much goes over paid transit compared with peering, how one customer or exchange point is
developing, where the network is filling up, and which path traffic takes.

> **Getting started:** installation, configuration, upgrades and backup/restore are covered in the
> [README](https://git.nordfiber.no/Nordfiber/bifrost/src/branch/main/README.md).
> Source code: [repository](https://git.nordfiber.no/Nordfiber/bifrost).

## How it fits together

Three services run under Docker Compose on one host. A **poller** talks SNMP to the routers, a **web
app** shows the graphs and maps, and **one PostgreSQL database** with TimescaleDB holds everything.

You give each interface a **role** (transit, peering, IX, PNI, CDN, core, distribution, NNI or
customer) and a **counterparty** (name and ASN). Bifrost suggests both from the interface description,
using an editable rule table that learns the names you enter; every graph and map is built on this.
The web interface is in Norwegian or English (set for the installation under Settings, and per user on My
page); this page gives the Norwegian labels in parentheses.

## Questions it answers

| Question | Where to look |
|---|---|
| How much Internet traffic do we have, and with whom? | Internet (Internett) |
| How much internal backbone traffic is there? | Backbone |
| Who are we exchanging traffic with right now? | Distribution (Fordeling) |
| Where is the network getting full, and is a link down? | Maps (Kart) |
| How does the network connect logically, and which path does traffic take? | Logical maps (Logisk) |
| What is the most traffic we have ever had? | Peaks (Trafikk-topper) |
| What did one interface look like months ago? | History (Historikk) |
| Can a NOC screen show maps and graphs without anyone logging in? | Info screens (Infoscreens) |

## The pages

| Page | What it is for |
|---|---|
| **Internet** (Internett) | Traffic leaving the network over time: IX, PNI, CDN, transit, peering and customers with their own ASN. The total counts only traffic going out of the network. |
| **Backbone** | Internal traffic: core, distribution, NNI and customers without an ASN, one line each. The same data as Internet, shown differently. |
| **Distribution** (Fordeling) | Your own network in the centre with the counterparties around it, line width showing volume. Grouped by ASN or by function, for now, the last hour or the last 24 hours. |
| **Maps** (Kart ▾) | Physical maps of the network, with each link coloured by load. |
| **Logical maps** (Logisk ▾) | OSPF maps that draw themselves, one per area, plus maps of routers you pick. |
| **Peaks** (Trafikk-topper, the «All-time ▸» button) | The highest traffic per series for today, this week, month and year, and all time. Shown for 5-minute spikes and for 1-hour sustained load, as a table or a trend graph. |
| **History** (Verktøy ▾ → Historikk) | One interface, in and out, going back in time or on a chosen date. |
| **Inventory** (Inventar) | The same host list and categories as Admin, read-only. |
| **Log** (Logg) | A permanent event log: logins, user and access changes, host and interface changes. |
| **Info screens** (Infoscreens) | Set up NOC screens that rotate through chosen maps and graphs. |
| **My page** (Min side) | Change your password, choose your display language, manage passkeys, see your logged-in devices and log them out. |
| **Admin** | Add hosts by SNMP discovery, classify interfaces and re-scan hosts. From here you reach Settings (Innstillinger), Users & groups (Brukere & grupper), Data quality (Datakvalitet) and Imported sites (Importerte sites). |
| **About** (Om, the version badge) | App, database and poller status. Operators and admins also see how much data is stored and how fresh it is. |

Graphs cover 36 hours (5-minute points), a week or a month (hourly points) and 12 to 48 months (daily
points). Drag across a graph to zoom in, and Bifrost fetches finer data for that span.

## Maps

**Physical maps** (Kart) show routers and the links between them. Links come from the interface
descriptions: an interface whose description names another host on the map becomes a link, and the
rest become links out to counterparties. Each half of a link shows its own end, coloured green, then
yellow, then red as it fills, so a link that is down on one side only is easy to spot. *Down* (red
«NEDE») is kept separate from *no fresh data* (grey «ingen data»). Hover over a link for rate and
capacity, click it for a traffic graph, or use the time slider to look back over the last 24 hours.

**Logical maps** (Logisk) are drawn from OSPF. Bifrost asks the routers it already polls who their
OSPF neighbours are, and draws what they report: routers as nodes, adjacencies as links with the cost
in each direction and the state of each half. You get one automatic map per OSPF area (Auto (pr.
area)), and you can make your own maps from routers you choose. Neighbours that Bifrost does not
monitor are shown dimmed as *ghosts*, which shows you your blind spots. The **routing lens**
(Trafikkflyt) highlights the path between two routers by OSPF cost, or by OSPF+LSP where an MPLS LSP
exists (LSP paths are calculated and marked as such). The OSPF picture is rebuilt daily.

Both kinds of map use **the same editor and the same placement engine**:

- **Auto** builds a tidy layout from scratch and gives the same result every time.
- **Forbedre** (improve) tidies the current layout with fewer crossings and better spacing.
- **Nytt** (new) builds from scratch another way, when you want a different suggestion.
- **Flip ⇋ / flop ⇅** mirror the whole layout east–west or north–south. Click again to undo.
- Drag, rename, lock or grid-snap nodes. Every automatic step leaves locked nodes where they are.

**GPS from NetBox** can be the starting point. *Relative* spreads the nodes out in proportion to where
they really are, and *exact* puts each one on its spot on a geographic background. It is only a
starting point: nothing is locked to it, and later steps can move nodes freely.

**Background maps** (Bakgrunnskart) sit behind the nodes as a separate layer: a map snapshot saved
with the map (offline from then on), live map tiles (needs internet), or your own image, faded to taste.

**Lifecycle.** A map has one **active** version (the one people see), at most one **saved draft**, and
a **working copy** while you edit. Edit (Rediger) opens the working copy, and the live map stays as it
is until you press Activate (Aktiver). Save draft (Lagre utkast) keeps your work for later. Discard
(Forkast) goes one step back: first it undoes unsaved changes, then it drops the working copy. Physical
maps also keep earlier versions that you can restore into the working copy. **Park** (Parker) takes a
map off the menu but keeps it as a draft, and **Archive** (Arkiver) hides it until you restore it.
Drafts and parked maps are removed after 28 days without activity by default. Archived maps are kept.

## Who sees what

Access is always set on a **group**, never on a single user. You log in with a password or a passkey,
and deactivating a user or resetting a password logs them out at once. There are three groups:

| Group | Sees | Can change |
|---|---|---|
| **brukere** (users; new accounts start here) | Nothing until the group is given access: chosen graphs, categories and customers, Distribution, public links, and individual maps | Only their own password, passkeys and devices. This group can never get write access. |
| **operatør** (operator) | Everything except Admin, including History, Inventory and Log | Creates maps (their group can then edit them), edits maps they have write access to, and edits info screens |
| **admin** | Everything | Everything |

**Maps are shared one at a time**, and physical and logical maps work the same way: a group gets read
or write access to exactly the maps you choose. Anyone who can see a logical map can also use its
routing lens. Only a few functions are held back: operators create maps, and only admins collect OSPF
and LSP data from the routers again. An admin can also **lock** a physical map so that everyone else
can only read it.

Pages look the same whatever your role; access decides which action buttons you get, and the API
follows the same rules. A common setup is a customer login that sees only that customer's traffic.

**Public links** (Public link) show a published map on a bare page that refreshes every five minutes.
You still have to log in, but you only need the separate "public" access, not access to each map.

**Info screens** get their own link that cannot be guessed and works **without logging in**, for a NOC
wall. The screen rotates through the physical maps and graphs you pick, each with its own caption and
display time. You can generate a new link at any time, and the old one stops working straight away.

## Where the data comes from, and how long it is kept

- The poller reads interface counters over SNMP (v1, v2c or v3) **every five minutes**, a fixed interval.
- Bifrost only **reads** over SNMP and never writes, so it needs read-only access on the routers and
  nothing more.
- It uses the **64-bit counters**. A 32-bit counter wraps in about 3.4 seconds at 10 Gbit/s, so 32-bit
  counters are used only for SNMPv1 devices, which cannot send 64-bit ones.
- Bifrost stores the **raw counters**, not rates. A rate is worked out when you look at it: the change
  in the counter × 8, divided by the time between readings.
- **Raw 5-minute readings are kept for 90 days**, then deleted. Hourly and daily averages are built
  alongside them and kept long-term, so graphs over months and years still work. Nothing stored is ever
  rewritten into a coarser form.
- **Peaks** are taken from the raw data daily and stored separately, so all-time highs outlast 90 days.
- Interfaces are tracked by **port name**, so Bifrost keeps up when a device renumbers its ifIndex,
  and history, maps and peaks carry on. A device reboot is spotted in the normal poll, and a re-scan of
  that device starts on its own.
- Extra SNMP requests such as re-scans and map builds are **rate-limited per device**, whoever
  triggers them. The five-minute poll runs on its own schedule.

**Impossible values.** Because rates are worked out from raw counters, a counter glitch or a change of
interface speed can produce one value that is physically impossible. A rate far above what the
interface can carry (its speed times an adjustable factor) is treated as a glitch and shown as a gap,
never as a false spike, so graphs and peaks stay correct. The raw data itself is not changed. On the
**Data quality** page (Datakvalitet), an admin can inspect, acknowledge, repair or clean up the rare
cases that need attention.

## NetBox

If you use NetBox, Bifrost can look devices up there when you add a host, and import each device's
**site and GPS position**. A site is stored once and shared by every device at that site. The GPS
position can then be the starting point for automatic placement on the maps. The **Imported sites** page
(Importerte sites) lists the sites read-only, because they are edited in NetBox. From there you can
update a single site from NetBox, or delete a site; its devices stay and just lose the link to it.

## Backups

Traffic data, configuration and users all live in the one database, so one backup covers everything.
Backups are **encrypted with a passphrase by default**, and you need the same passphrase to restore.
Unencrypted backups are possible, but only if you ask for them explicitly. See the README for details.

## Further reading

- [README](https://git.nordfiber.no/Nordfiber/bifrost/src/branch/main/README.md): installation,
  configuration, upgrades, backup and restore.
- [GUI recipe](https://git.nordfiber.no/Nordfiber/bifrost/wiki/bifrost-gui-oppskrift): how the calm,
  readable interface is built — a handful of principles, design tokens and components. It is written as
  an example you can reuse in other projects.
- The repository's `docs/` folder also has a complete colour theme (`theme.example.css`) and an nginx
  example with HTTPS (`nginx.conf.example`) to start from.

## Author

Roger Jorgensen — <rogerj@gmail.com>

Design, architecture and structure; the data model, and the choice to store the raw counters and work
out rates when they are read; how traffic is combined and presented, in particular the Distribution page
and the automatic generation and placement of network maps; and the decisions about what Bifrost does
and deliberately does not do: one database, no online services, a fixed five-minute poll, and nothing
destructive.

The code is written by Claude AI (Opus and Fable).

Reviewed independently by Qwen, DeepSeek, Fable, Opus, Gemini and MiMo.

## Licence

Bifrost is licensed under the **GNU AGPL v3 or later** (`AGPL-3.0-or-later`). You can use, change and
share it freely. If you offer a modified version to others, including as a network service, your
changes must be made available under the same licence. A **commercial licence** without the AGPL
obligations is available on request: contact <rogerj@gmail.com>.
