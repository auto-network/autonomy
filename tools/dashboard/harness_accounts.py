"""The launch chooser's account list (bead auto-k784w; design
graph://7eb29bc8-31a v6 §11, Delta 1 and 3).

``account_rows(harness)`` is every account of a harness in this machine's
vault, as the chooser shows it: identity, plan, whether it can launch,
whether its reading says it is used up, the reading itself, its source, and
whether the launcher's own picker would choose it. No secret part is ever
read into a row. ``check_account`` is the strict check a launch naming an
account runs before anything starts.

Organization-shared accounts (auto-26e8a) are listed beside the personal
ones with their organization as ``source``; their usage readings are
auto-elxua. Only a personal account is ever ``recommended``: the auto-pick
never spends an organization's shared account.
"""

from __future__ import annotations

from datetime import datetime, timezone

ACCOUNT_NOT_FOUND = "account-not-found"
ACCOUNT_NOT_LAUNCHABLE = "account-not-launchable"
#: The only account parts a row carries; every other part may be a secret.
PUBLIC_PARTS = ("alias", "email", "org_name")


def _readings(harness: str) -> dict[str, dict]:
    """This machine's usage readings for *harness*, by account id."""
    from tools.dashboard import harness_usage_settings as hus
    from tools.graph import settings_ops

    try:
        members = settings_ops.read_set(hus.HARNESS_USAGE_SET_ID, org="personal", peers=[])
    except Exception:
        return {}
    out = {}
    for member in members.members:
        payload = member.payload
        if isinstance(payload, dict) and (payload.get("harness") or "").lower() == harness \
                and isinstance(payload.get("account_id"), str):
            out[payload["account_id"]] = payload
    return out


def _usage_view(reading: dict | None) -> dict | None:
    if not isinstance(reading, dict):
        return None
    windows = reading.get("windows") if isinstance(reading.get("windows"), dict) else {}
    view = {name: {"used_percent": w.get("used_percent"), "resets_at": w.get("resets_at")}
            for name, w in windows.items()
            if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))}
    if not view:
        return None
    return {**view, "as_of": reading.get("updated_at"), "status": reading.get("status"),
            "source": reading.get("source")}


def _recommended(harness: str, launchable: list) -> str | None:
    """The account the launcher's own picker would choose, or None when it
    would pick at random. The picker's decision alone: no credential is
    opened."""
    import random

    from agents import session_launcher as sl

    if len(launchable) == 1:
        return launchable[0].id
    if harness != "claude" or not launchable:
        return None
    chosen, selection = sl._choose_claude_account(
        launchable, prefer_alias=None, account_id=None, rng=random.Random(0))
    if chosen is None or str(selection.get("method", "")).startswith("random"):
        return None
    return chosen.id


def account_rows(harness: str) -> list[dict]:
    from agents import session_launcher as sl
    from tools.graph import harness_credentials as hv

    accounts = hv.all_accounts(harness)
    readings = _readings(harness)
    now = datetime.now(timezone.utc)
    launchable = [a for a in accounts if a.launchable and a.source == hv.PERSONAL]
    recommended = _recommended(harness, launchable)
    rows = []
    for acct in accounts:
        reading = readings.get(acct.id) if acct.source == hv.PERSONAL else None
        rows.append({
            "account_id": acct.id,
            "harness": harness,
            **{part: acct.get(part) for part in PUBLIC_PARTS},
            "plan_type": (reading or {}).get("plan_type"),
            "source": acct.source,
            "launchable": acct.launchable,
            "openable": acct.openable,
            "exhausted": bool(reading) and sl._usage_exhausted(reading, now=now),
            "recommended": acct.source == hv.PERSONAL and acct.id == recommended,
            "usage": _usage_view(reading),
        })
    rows.sort(key=lambda r: (not r["recommended"], r["exhausted"], not r["launchable"],
                             (r["alias"] or r["account_id"]).lower()))
    return rows


def check_account(harness: str, account_id: str,
                  org: str | None = None) -> tuple[str, str] | None:
    """``None`` when *account_id* is a launchable *harness* account here (in
    organization *org*'s shared set when given); else ``(refusal code,
    detail)``."""
    from tools.graph import harness_credentials as hv

    acct = next((a for a in hv.list_accounts(harness, org=org) if a.id == account_id), None)
    if acct is None:
        where = f"organization {org}'s shared accounts" if org else "this vault"
        return ACCOUNT_NOT_FOUND, f"no {harness} account {account_id} in {where}"
    if not acct.launchable:
        return ACCOUNT_NOT_LAUNCHABLE, (
            f"the {harness} account {account_id} cannot launch "
            + ("(the vault is not open)" if not acct.openable else "(no usable sign-in)"))
    return None
