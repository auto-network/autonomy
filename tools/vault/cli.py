"""Headless CLI for the vault policy-class lifecycle (crib §21).

The full protocol driven with throwaway identities, no browser and no
human-entered password — the primary functional acceptance surface for bead
auto-39d26. Every command prints its result as one JSON object (the "view
state": data displayed after the transition), so the CLI IS the API shape.

THIS IS NOT THE PRODUCT SURFACE. The epic is explicit: "There is no separate
vault service, no ``vault get`` command." Secrets reach a caller through
``graph set read``, which returns a payload, an access error, or a notice. This
module exists to drive acceptance headlessly and must not be registered as a
console entry point or documented as a way to read a secret.

Run ``python3 -m tools.vault.cli demo`` for the whole path end-to-end; the
granular subcommands drive one transition each against a ``--store`` file.

VIEW-STATE MAP (data entered → data displayed):

  enroll-password   enter: factor-id, password          show: {factor_id, public_key}
  create-class      enter: policy, factor-ids           show: {class_id, policy, factor_ids}
  seal-setting      enter: name, class, genesis, policy  show: {setting_name, class_id, sealed}
  open-setting      enter: name, opener                 show: {setting_name, cek}
  enroll-into-class enter: class, opener, new factor    show: {class_id, wraps, added}
  revoke            enter: class, factor-id             show: {class_id, revoked, survivors}
  reseal            enter: name, opener                 show: {setting_name, resealed:true}
  show-class        enter: class                        show: the class record
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import service
from .errors import VaultError
from .store import VaultStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _openers(store: VaultStore, spec: str) -> dict[str, bytes]:
    """Parse ``id:password[,id:password...]`` into ``{factor_id: seed}``."""
    seeds: dict[str, bytes] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise VaultError(f"opener {pair!r} must be factor_id:password")
        fid, password = pair.split(":", 1)
        seeds[fid] = service.password_seed(store, fid, password)
    if not seeds:
        raise VaultError("at least one opener is required")
    return seeds


def _emit(obj: dict) -> None:
    print(json.dumps(obj, sort_keys=True))


def cmd_enroll_password(store: VaultStore, a) -> dict:
    published = service.enroll_password_factor(store, a.factor_id, a.password)
    return {"factor_id": published.factor_id, "public_key": published.public_key}


def cmd_create_class(store: VaultStore, a) -> dict:
    factor_ids = [f for f in a.factors.split(",") if f]
    class_id = service.create_policy_class(store, a.policy, factor_ids, created_at=_now())
    return {"class_id": class_id, "policy": a.policy, "factor_ids": factor_ids}


def cmd_seal_setting(store: VaultStore, a) -> dict:
    service.seal_setting(store, a.name, a.klass, a.genesis, a.policy)
    return {"setting_name": a.name, "class_id": a.klass, "sealed": True}


def cmd_open_setting(store: VaultStore, a) -> dict:
    cek = service.open_setting(store, a.name, _openers(store, a.opener))
    return {"setting_name": a.name, "cek": cek.hex()}


def cmd_enroll_into_class(store: VaultStore, a) -> dict:
    service.enroll_into_class(
        store,
        a.klass,
        _openers(store, a.opener),
        a.new_factor_id,
        new_password=a.new_password,
    )
    record = store.get_class(a.klass)
    return {"class_id": a.klass, "added": a.new_factor_id, "wraps": len(record.current().wraps)}


def cmd_revoke(store: VaultStore, a) -> dict:
    service.revoke_and_rekey(store, a.klass, a.factor_id, created_at=_now())
    record = store.get_class(a.klass)
    return {
        "class_id": a.klass,
        "revoked": a.factor_id,
        "survivors": list(record.factor_ids()),
    }


def cmd_reseal(store: VaultStore, a) -> dict:
    service.reseal_setting(store, a.name, _openers(store, a.opener))
    return {"setting_name": a.name, "resealed": True}


def cmd_show_class(store: VaultStore, a) -> dict:
    return store.get_class(a.klass).to_dict()


def cmd_demo(store: VaultStore, a) -> dict:
    """The whole acceptance path, printing each view state as it goes."""
    from .testkit import make_test_genesis

    genesis = make_test_genesis()
    _emit({"step": "enroll", **cmd_enroll_password(store, _ns(factor_id="pw-1", password="alpha"))})
    _emit({"step": "create-class", **cmd_create_class(store, _ns(policy="password", factors="pw-1"))})
    class_id = store.db.execute("SELECT class_id FROM policy_classes").fetchone()[0]

    s1 = service.seal_setting(store, "setting.a", class_id, genesis, "password")
    s2 = service.seal_setting(store, "setting.b", class_id, genesis, "password")
    _emit({"step": "seal-two-settings", "class_id": class_id, "a": s1.hex()[:16], "b": s2.hex()[:16]})

    o1 = service.open_setting(store, "setting.a", _openers(store, "pw-1:alpha"))
    assert o1 == s1, "setting.a data key did not open through its class"
    _emit({"step": "open-setting.a", "opens": o1 == s1})

    # enroll a second factor: one added wrap, settings' ciphertext unchanged
    before = store.get_secret("setting.a").sealed_cek
    service.enroll_into_class(store, class_id, _openers(store, "pw-1:alpha"), "pw-2", new_password="bravo")
    after = store.get_secret("setting.a").sealed_cek
    _emit({"step": "enroll-second-factor", "wraps": len(store.get_class(class_id).current().wraps), "ciphertext_unchanged": before == after})

    # the new factor alone opens both settings — they share one class key
    b_via_new = service.open_setting(store, "setting.b", _openers(store, "pw-2:bravo"))
    _emit({"step": "new-factor-opens-both", "b_opens": b_via_new == s2})

    # revoke pw-1, rekey, both settings follow at their next write
    service.revoke_and_rekey(store, class_id, "pw-1", created_at=_now())
    service.reseal_setting(store, "setting.a", _openers(store, "pw-2:bravo"))
    service.reseal_setting(store, "setting.b", _openers(store, "pw-2:bravo"))
    a_after = service.open_setting(store, "setting.a", _openers(store, "pw-2:bravo"))
    b_after = service.open_setting(store, "setting.b", _openers(store, "pw-2:bravo"))
    _emit({"step": "rotate-and-follow", "a_ok": a_after == s1, "b_ok": b_after == s2, "revoked_survivors": list(store.get_class(class_id).factor_ids())})

    return {"demo": "ok", "class_id": class_id}


class _ns:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vault", description=__doc__)
    p.add_argument("--store", default=":memory:", help="SQLite store path")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll-password")
    e.add_argument("--factor-id", required=True)
    e.add_argument("--password", required=True)
    e.set_defaults(fn=cmd_enroll_password)

    c = sub.add_parser("create-class")
    c.add_argument("--policy", default="password")
    c.add_argument("--factors", required=True, help="comma-separated factor ids")
    c.set_defaults(fn=cmd_create_class)

    s = sub.add_parser("seal-setting")
    s.add_argument("--name", required=True)
    s.add_argument("--class", dest="klass", required=True)
    s.add_argument("--genesis", required=True)
    s.add_argument("--policy", default="password")
    s.set_defaults(fn=cmd_seal_setting)

    o = sub.add_parser("open-setting")
    o.add_argument("--name", required=True)
    o.add_argument("--opener", required=True)
    o.set_defaults(fn=cmd_open_setting)

    x = sub.add_parser("enroll-into-class")
    x.add_argument("--class", dest="klass", required=True)
    x.add_argument("--opener", required=True)
    x.add_argument("--new-factor-id", required=True)
    x.add_argument("--new-password", required=True)
    x.set_defaults(fn=cmd_enroll_into_class)

    r = sub.add_parser("revoke")
    r.add_argument("--class", dest="klass", required=True)
    r.add_argument("--factor-id", required=True)
    r.set_defaults(fn=cmd_revoke)

    rs = sub.add_parser("reseal")
    rs.add_argument("--name", required=True)
    rs.add_argument("--opener", required=True)
    rs.set_defaults(fn=cmd_reseal)

    sc = sub.add_parser("show-class")
    sc.add_argument("--class", dest="klass", required=True)
    sc.set_defaults(fn=cmd_show_class)

    d = sub.add_parser("demo")
    d.set_defaults(fn=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    with VaultStore(args.store) as store:
        try:
            result = args.fn(store, args)
        except VaultError as exc:
            _emit({"error": type(exc).__name__, "message": str(exc)})
            return 1
    if args.cmd != "demo":
        _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
