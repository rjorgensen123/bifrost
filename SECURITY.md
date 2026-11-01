# Security Policy

## Before you report

Bifrost has been reviewed independently several times — by Qwen, DeepSeek, Fable, Opus, Gemini and
MiMo among others — with a person checking every finding against the code before anything was changed
or set aside, and with the reasoning written down.

Those reviews found real things, and they were fixed. That is not a claim that nothing is left. Real
findings are genuinely welcome, and Bifrost is better for the ones that have arrived.

What it does mean is that the obvious pass has already been run. A report that reads like the output
of a first look — a vulnerability class named in general terms, no file, no line, no reproduction —
has in all likelihood been produced and answered already. What is useful is precisely what a general
pass does not produce: a specific claim about this code, checked against it.

## What Bifrost is built on

Bifrost does not handle truly sensitive data. It stores interface counters, topology and the SNMP
credentials it polls with, and it **only reads** over SNMP — it never writes, so it needs read-only
access on the devices and nothing more. The SNMP credentials are stored in plain text on purpose: the
poller needs them in plain text, and a key on the same disk would add nothing. The protection is the
surroundings — an encrypted disk, `chmod 600 .env`, encrypted backups, TLS in front — as described
under Security in the [README](README.md). Please weigh a finding against that premise.

## Supported versions

The latest release. Older versions get no fixes — if you are on one, the answer to a vulnerability is
to upgrade. The version is shown in the footer, on the About page and at `/healthz`.

## Reporting a vulnerability

**Do not open a public issue.**

Email **rogerj@gmail.com** with `Bifrost` in the subject line.

## What a report must contain

A report is read when it shows that someone looked at this code. That means:

- **The file and line.** Which function, in which file, at which version or commit.
- **What the code actually does there** — not what a name suggests it does.
- **Why that is wrong**, stated as a concrete consequence: what an attacker gets, or what guarantee
  fails, and under what conditions.
- **How to see it.** A reproduction, a test, or a precise sequence of requests. If it cannot be
  reproduced, say so and explain what you did observe.

A proof of concept helps but is not required. A clear description of a real flaw is worth more than a
broken exploit.

## Reports produced with AI assistance

Use whatever tools you like to find a problem. But **a person must have checked the finding against
this code before it is sent, and that person must be able to answer questions about it.**

The reason is arithmetic, not principle. A plausible-sounding report costs minutes to produce and
hours to disprove: the file has to be read, the claim traced, the behaviour reproduced, and the
conclusion written down. A handful of unverified reports can consume more maintainer time than every
real vulnerability this project will ever have — and they crowd out the real ones.

So no output pasted straight from a model. A report that reads as unreviewed generated text will be
closed without analysis and without a second look. The usual signs:

- a claim with no file and no line
- code quoted that does not exist in this repository
- a reproduction that does not run, or that tests something else
- a vulnerability class named in the abstract, with nothing tying it to this code
- confident language and no evidence

If you did use a tool, say so. That is not held against you — it is useful context, and an honest
report that names its method is treated exactly like any other. What is held against you is sending
something you have not verified yourself.

## What to expect

This is a spare-time project with a single maintainer. **There is no guaranteed response time.** You
will get an answer when there is one, and it will say which of these it is: a fix, a documented
limitation, or an explanation of why the behaviour is not what it appears to be.

A fix will not be published without telling you first, and you will be credited unless you ask not to
be.

## Scope

Bifrost builds on established components — Flask, PostgreSQL with TimescaleDB, psycopg2, pysnmp,
gunicorn, htmx, uPlot and Leaflet among them. Every dependency is pinned to an exact version and
checked for new releases.

**A flaw in one of those is not Bifrost's to fix.** Report it to that project — that is where a fix
can be made, and where every other user of it is waiting.

**But say that it exists.** Bifrost is exposed to it, and that is worth knowing even though the repair
happens elsewhere: a version can be pinned or upgraded, or the documentation can say what not to rely
on until it is fixed. An issue saying *"this dependency has this problem, here is the upstream
report"* is enough — no analysis needed.

What is in scope here is how Bifrost uses them:

- access control: that every route, API and page lets a user reach exactly what they have access to
- login, sessions, passkeys, the break-glass admin and CSRF protection
- how input is checked before it is used, and what is written and returned
- template escaping and the Content Security Policy
- how the poller and the web app talk SNMP to the devices

**Not in scope:** that SNMP credentials are stored in plain text (deliberate, see above), that a
chosen design is not the one you would have chosen, that a dependency has a newer version, or that a
documented and deliberate limitation exists. Those are issues, not vulnerabilities, and they are
welcome as such.
