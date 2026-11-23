# Quick start

Bifrost shows the traffic in a **network of routers that are connected to each other**. It is not built
around single routers and interfaces: every interface you monitor gets a **category**, and the graphs
and maps show the traffic per category, summed across the routers.

You decide what is in that picture by choosing interfaces. That choice is the important part.

## Categories

Each interface you monitor has a **role** (rolle) and a **counterparty** (motpart).

| Role | Shown as |
|---|---|
| transit, peering, IX, PNI, CDN | **Internet** (Internett): the traffic out of your network, in total and per category and counterparty |
| core, distribution, NNI | **Backbone**: your own network, one line per role |
| customer | Per customer |
| uncategorised (ukategorisert) | Not in the category graphs and not on the maps |

## Choose the interfaces that carry the traffic you want

A graph is the sum of every interface you have chosen in that category, so **each traffic flow must be
chosen once**.

- `xe-0/0/0` together with `xe-0/0/0.123` counts the traffic on unit 123 twice, because the port
  already includes its units. Choose the port, or the units you want, not both.
- The same goes for a bundle (`ae0`, Port-channel) and its member ports.
- Units and other internal interfaces are hidden in the list until you tick **Show N internal/virtual
  interfaces**.

## Getting started

1. Admin → **Discover via SNMP** (Oppdag via SNMP): enter the router's IP address and SNMP details.
2. Go through the interface list: tick the interfaces you want, check the role and counterparty
   Bifrost suggests from the descriptions, and save.
3. Do the same for the other routers. After two polls, about ten minutes, Internet and Backbone show
   the sums.
4. Maps (Kart) → **Create map** (Lag kart) shows the routers and the links between them.

Installation is in the [README](https://github.com/rjorgensen123/bifrost/blob/main/README.md#quick-start),
and every page is described in [the overview](https://github.com/rjorgensen123/bifrost/blob/main/docs/Home.md).
