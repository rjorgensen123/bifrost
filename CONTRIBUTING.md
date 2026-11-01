# Contributing

## This repository is a mirror

The work happens elsewhere, and this repository is a copy that is pushed here. Every
sync overwrites what is here.

That has a consequence worth knowing before you spend time on it: **a pull request
opened here cannot be merged, and anything committed here is overwritten at the next
sync.** That is not a judgement on the change — the mechanism has nowhere to put it.

So if you have a change, it has to arrive in a form that survives the sync. An issue
with a description, a diff, or a patch works. A pull request does not.

## What is welcome

**Bug reports.** If something does not behave as the documentation says, or a
guarantee does not hold, open an issue. These get looked at first.

**Questions about the documentation.** If the README or the documentation is unclear
or wrong about what Bifrost does, that is worth an issue on its own.

**Security findings.** Not as an issue — see [SECURITY.md](SECURITY.md), which also
says what a report must contain.

## Code and features

Neither is refused. Both are low priority.

**Code.** If you have written something, describe it in an issue and attach the diff.
It will be read. Whether it lands, and when, depends on whether it fits what Bifrost
is for — and on time, of which there is not much. Do not expect a quick answer, and do
not take silence as rejection.

Code is accepted only under Bifrost's own terms: it must be possible to distribute it
under AGPL-3.0-or-later **and** under Bifrost's commercial license. By submitting code
for inclusion you agree to that. See [License](README.md#license) in the README for
what that means.

**Feature requests.** Possible, and worth asking about. Say what you are trying to do
rather than which button you want — the problem is easier to judge than the solution.

## If you are filing an issue

Say which version. It is shown in the footer, on the About page and at `/healthz`.

Include what someone else needs to see the same thing: what you did, the input, and
the actual output or error. A failing test is the clearest form there is.
