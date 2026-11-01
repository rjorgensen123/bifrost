# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks of what the map editors send (node and link lists, map settings).

Everything is checked before anything is written. A value of the wrong type, a node or link that is
not on the map, or a number that is not finite is rejected with a PayloadError whose message is meant
for the user; nothing is guessed at, skipped or coerced. The caller logs the rejection and answers 400.
"""
import logging
import math

import i18n

log = logging.getLogger("bifrost.maps")


class PayloadError(ValueError):
    """The request body does not have the expected shape; str(error) is the message for the user."""


def bad_field(field):
    return PayloadError(i18n.t("msg.maps.bad_field", field=field))


def finite(value):
    """A finite int or float (never a bool, text, NaN or infinity) as float, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def node_list(raw, members, id_key, id_type, cap=None):
    """Validate a list of nodes as the editors send it.

    Each node is an object with `id_key` (an `id_type` that is one of `members`), finite `x` and `y`,
    and optionally `pinned` (bool), `label` (text or null) and `size` (S, M, L or null). Returns
    [{"id", "x", "y", "pinned"} + "label"/"size" when sent], or raises PayloadError."""
    if not isinstance(raw, list):
        raise bad_field("nodes")
    if cap is not None and len(raw) > cap:
        raise PayloadError(i18n.t("msg.maps.too_many_nodes", n=len(raw), cap=cap))
    out, seen = [], set()
    for n in raw:
        if not isinstance(n, dict):
            raise bad_field("nodes")
        for k in (id_key, "x", "y"):
            if k not in n:
                raise PayloadError(i18n.t("msg.maps.node_missing_field", field=k))
        nid = n[id_key]
        if isinstance(nid, bool) or not isinstance(nid, id_type) or nid in seen:
            raise bad_field(id_key)
        if nid not in members:
            raise PayloadError(i18n.t("msg.maps.not_on_map", id=nid))
        seen.add(nid)
        x, y = finite(n["x"]), finite(n["y"])
        if x is None or y is None:
            raise PayloadError(i18n.t("msg.maps.node_bad_xy"))
        pinned = n.get("pinned", False)
        if not isinstance(pinned, bool):
            raise bad_field("pinned")
        item = {"id": nid, "x": x, "y": y, "pinned": pinned}
        if "label" in n:
            if n["label"] is not None and not isinstance(n["label"], str):
                raise bad_field("label")
            item["label"] = n["label"]
        if "size" in n:
            if n["size"] not in (None, "S", "M", "L"):
                raise bad_field("size")
            item["size"] = n["size"]
        out.append(item)
    return out


def link_list(raw, members, cap=None):
    """Validate a list of links: objects with an int `id` that is one of `members` and a bool
    `enabled`. Returns [(enabled, id)], or raises PayloadError."""
    if not isinstance(raw, list):
        raise bad_field("links")
    if cap is not None and len(raw) > cap:
        raise PayloadError(i18n.t("msg.maps.too_many_links", n=len(raw), cap=cap))
    out, seen = [], set()
    for link in raw:
        if not isinstance(link, dict):
            raise bad_field("links")
        for k in ("id", "enabled"):
            if k not in link:
                raise PayloadError(i18n.t("msg.maps.link_missing_field", field=k))
        lid = link["id"]
        if isinstance(lid, bool) or not isinstance(lid, int) or lid in seen:
            raise bad_field("id")
        if lid not in members:
            raise PayloadError(i18n.t("msg.maps.not_on_map", id=lid))
        if not isinstance(link["enabled"], bool):
            raise bad_field("enabled")
        seen.add(lid)
        out.append((link["enabled"], lid))
    return out


def reject(map_id, msg):
    """Log a rejected editor request (who, which map, what) and answer 400; nothing was written."""
    from flask import jsonify, session
    who = session.get("uname") or ("admin" if session.get("admin") else "?")
    log.warning("map %s: editor request rejected (%s): %s", map_id, who, msg)
    return jsonify(error="bad_payload", msg=msg), 400


def json_list(text):
    """The `nodes` form field of an auto-layout request: JSON text holding a list."""
    import json
    try:
        value = json.loads(text or "[]")
    except ValueError:
        raise PayloadError(i18n.t("msg.maps.bad_json")) from None
    return value
