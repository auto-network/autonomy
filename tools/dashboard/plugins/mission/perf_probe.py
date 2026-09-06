"""Phase-timing probe for the mission screen's loading interstitial.

The dashboard logs no request durations, so this is the only way to see
where a mission open spends its time. Two modes, both bounded and safe:

``live <mission_id>``
    Streams ``/api/mission/screen/<id>?progress=1`` from the running
    dashboard (bearer auth from ``$CROSSTALK_TOKEN``) and stamps the
    arrival of every ``<!--msn:pct|note-->`` stage marker. The gap after
    a marker is the time the server spent in the phase it names. Then it
    times the ``bd`` invocations the bead bridge (``bridge.py``) makes
    for that mission, one by one, so the "Bridging beads" phase can be
    attributed to list / show / per-bead comments.

``local <mission_id>``
    Captures the live document once, seeds a throwaway settings store
    under ``/tmp`` with its mission rows (padded to the live row count),
    stubs the bead bridge with the captured payload, and times every
    phase in-process: the algorithmic floor, with nothing else running.
    A live phase far above its local floor is host contention, not the
    plugin's own cost.

Example::

    python3 -m tools.dashboard.plugins.mission.perf_probe live <mission_id>
    python3 -m tools.dashboard.plugins.mission.perf_probe local <mission_id>
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request

_MARK = re.compile(rb"<!--msn:(\d+)\|([^>]*?)-->|<!--msn:doc:(\d+)-->")
_LOCAL_ROOT = "/tmp/mission-perf-probe"


def _api() -> str:
    return os.environ.get("GRAPH_API", "https://localhost:8080").rstrip("/")


def _open(url: str):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    token = os.environ.get("CROSSTALK_TOKEN", "")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"} if token else {})
    return urllib.request.urlopen(req, context=ctx)


def _table(title: str, events: list[tuple[str, float]]) -> None:
    """events: (label, absolute seconds); prints the gap AFTER each label."""
    print(f"--- {title}")
    print(f"{'phase (time spent after this marker)':<54}{'at':>9}{'ms':>8}")
    for i, (label, at) in enumerate(events):
        nxt = events[i + 1][1] if i + 1 < len(events) else at
        print(f"{label[:52]:<54}{at*1000:>7.0f}ms{(nxt-at)*1000:>8.0f}")
    print(f"TOTAL {events[-1][1]*1000:.0f}ms")


def stream_phases(mission_id: str) -> tuple[list[tuple[str, float]], bytes]:
    t0 = time.perf_counter()
    resp = _open(f"{_api()}/api/mission/screen/{mission_id}?progress=1")
    events = [("headers", time.perf_counter() - t0)]
    buf = b""
    seen = 0
    while True:
        chunk = resp.read1(65536)          # whatever is available, no waiting
        if not chunk:
            break
        buf += chunk
        for m in _MARK.finditer(buf, seen):
            label = (m.group(2).decode() if m.group(2)
                     else f"doc marker ({m.group(3).decode()} bytes)")
            events.append((label, time.perf_counter() - t0))
            seen = m.end()
    events.append((f"end ({len(buf)} bytes)", time.perf_counter() - t0))
    return events, buf


def _bd(args: list[str], env: dict) -> tuple[float, str]:
    t = time.perf_counter()
    out = subprocess.run(["bd", *args, "--json"], capture_output=True,
                         text=True, env=env, timeout=120).stdout
    return time.perf_counter() - t, out


def bd_breakdown(mission_id: str, org: str) -> None:
    """The bridge's own bd calls, timed individually."""
    from tools.dashboard.plugins.mission import bridge
    env = bridge._beads_env(org)
    dt, out = _bd(["list", "--label", f"mission:{mission_id}", "--all"], env)
    rows = json.loads(out) if out.strip() else []
    ids = [r["id"] for r in rows if r.get("id")]
    with_comments = [r["id"] for r in rows if r.get("comment_count")]
    print(f"--- bd calls the bridge makes for {len(ids)} beads")
    print(f"{'bd list --label mission:<id> --all':<54}{dt*1000:>8.0f}ms"
          f"  {len(out)} bytes")
    if ids:
        dt, out = _bd(["show", *ids], env)
        print(f"{'bd show <all ids>':<54}{dt*1000:>8.0f}ms  {len(out)} bytes")
    t = time.perf_counter()
    for bid in with_comments:
        _bd(["comments", bid], env)
    dt = time.perf_counter() - t
    print(f"{f'bd comments x{len(with_comments)} (serial)':<54}{dt*1000:>8.0f}ms")


def _blk(doc: str, id_: str):
    m = re.search(
        r'<script (?:id="%s" type="application/json"|type="application/json"'
        r' id="%s")>(.*?)</script>' % (id_, id_), doc, re.S)
    return json.loads(m.group(1).replace("<\\/", "</")) if m else None


def local_phases(mission_id: str) -> None:
    doc = _open(f"{_api()}/api/mission/screen/{mission_id}").read().decode()
    data, beads = _blk(doc, "mc-data"), _blk(doc, "mc-beads") or {}
    org = data["mission"]["org"]
    os.environ["AUTONOMY_DATA_ROOT"] = _LOCAL_ROOT
    shutil.rmtree(_LOCAL_ROOT, ignore_errors=True)
    os.makedirs(f"{_LOCAL_ROOT}/orgs")
    from tools.graph import settings_ops as so
    from tools.graph.db import GraphDB
    import tools.dashboard.plugins.mission.entrypoints.schemas  # noqa: F401
    from tools.dashboard.plugins.mission import compose
    GraphDB(f"{_LOCAL_ROOT}/orgs/{org}.db", create=True).close()
    so.add_setting("mission.registry", 1, mission_id,
                   {"name": data["mission"]["name"], "status": "active"},
                   org=org)
    for p in data["pillars"]:
        so.add_setting("mission.pillar", 1, f"{mission_id}:{p['pillar_id']}",
                       {"name": p["name"], "color": p["color"] or "#888888",
                        "order": 1.0, "coordinator_session": "probe",
                        "bead_labels": [f"pillar:{p['pillar_id']}"]}, org=org)
    derived = {"surface_id", "item_id", "key", "created_at", "updated_at"}
    n = 0
    for mid in (mission_id, "pad-mission-a", "pad-mission-b"):
        for it in data["items"]:
            so.add_setting("mission.item", 1,
                           f"{mid}:{it['surface_id']}:{it['item_id']}",
                           {k: v for k, v in it.items() if k not in derived},
                           org=org)
            n += 1
    print(f"seeded {n} item rows + pillars/registry into {_LOCAL_ROOT}")
    compose.load_beads = lambda *_a, **_k: beads
    for run in (1, 2):
        events = []
        t0 = time.perf_counter()
        for chunk in compose.render_stages(org, mission_id):
            m = _MARK.match(chunk.encode())
            label = (m.group(2).decode() if m and m.group(2)
                     else "doc marker" if m else f"document ({len(chunk)} B)")
            events.append((label, time.perf_counter() - t0))
        _table(f"local compose, run {run} (bead bridge stubbed)", events)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("live", "local"):
        print(__doc__)
        return 2
    mode, mission_id = argv
    if mode == "live":
        events, buf = stream_phases(mission_id)
        _table(f"live stream {mission_id}", events)
        doc = buf.decode(errors="replace")
        data = _blk(doc, "mc-data")
        if data:
            bd_breakdown(mission_id, data["mission"]["org"])
    else:
        local_phases(mission_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
