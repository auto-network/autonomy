"""The capability gate under concurrent threads (auto-czoc0 node acceptance)."""

import collections
import threading

from tools.dashboard.tests.test_capability_gate import HEADER, env, grant  # noqa: F401 - fixture
from tools.dashboard import capability_gate as gate


def test_require_capability_is_correct_under_concurrent_threads(env):  # noqa: F811
    grant()
    outcomes = collections.Counter()
    start = threading.Barrier(16)

    def worker():
        start.wait()
        for _ in range(50):
            try:
                gate.require_capability(HEADER, "browser")
                outcomes["ok"] += 1
            except gate.CapabilityRefused as exc:
                outcomes[f"refused {exc.status}: {exc.detail[:60]}"] += 1
            except Exception as exc:  # noqa: BLE001 - the defect under test
                outcomes[f"error {type(exc).__name__}: {str(exc)[:60]}"] += 1

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes == {"ok": 800}, dict(outcomes)
