"""รวมจาน: queued orders' items grouped by dish plus the same set of options (SPEC §3.4)."""

from __future__ import annotations

from typing import Any


def batch_groups(queued: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One group per (dish, option set): total quantity, queue numbers, and per-order notes.

    `queued` comes oldest first (earlier shop days, then today by queue number). Queue numbers
    repeat across days, so each order also gets a `ref` (`#5 (เมื่อวาน)` for earlier days).
    Done/cancelled orders aren't passed in.
    """
    groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for order in queued:
        q = int(order["queue_number"])
        ref = f"#{q}" if order.get("is_today", True) else f"#{q} ({order.get('day_label', '')})"
        for line in order["items"]:
            key = (str(line["dish_id"]), tuple(sorted(str(x) for x in line.get("option_ids", []))))
            group = groups.setdefault(
                key,
                {
                    "dish_id": key[0],
                    "dish_name_th": line.get("dish_name_th", key[0]),
                    "option_names_th": list(line.get("option_names_th") or []),
                    "qty": 0,
                    "orders": [],
                    "refs": [],
                    "notes": [],
                    "_ids": set(),
                },
            )
            group["qty"] += int(line.get("qty") or 0)
            if order.get("id") not in group["_ids"]:
                group["_ids"].add(order.get("id"))
                group["orders"].append(q)
                group["refs"].append(ref)
            note = str(line.get("note") or "").strip()
            if note:
                group["notes"].append({"queue_number": q, "ref": ref, "note": note})
    for group in groups.values():
        del group["_ids"]
    return list(groups.values())
