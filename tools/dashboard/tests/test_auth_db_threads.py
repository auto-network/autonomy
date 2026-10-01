"""auth_db serves concurrent threads (found by auto-czoc0's node acceptance)."""

import hashlib
import threading

from tools.dashboard.dao import auth_db


def test_token_resolution_is_correct_under_concurrent_threads(tmp_path):
    auth_db.init_db(tmp_path / "auth.db")
    tokens = {f"tok-{i}": f"auto-{i}" for i in range(8)}
    for token, session in tokens.items():
        auth_db.insert_token(hashlib.sha256(token.encode()).hexdigest(), session, "org")
    errors, wrong = [], []
    start = threading.Barrier(16)

    def worker(n):
        start.wait()
        for i in range(300):
            token = f"tok-{(n + i) % 8}"
            try:
                got = auth_db.resolve_token(hashlib.sha256(token.encode()).hexdigest())
            except Exception as exc:  # noqa: BLE001 - the defect under test
                errors.append(repr(exc))
                continue
            if got != (tokens[token], "org"):
                wrong.append((token, got))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and wrong == [], (errors[:3], wrong[:3], len(errors), len(wrong))


def test_short_lived_threads_leave_no_open_handles(tmp_path):
    # anyio worker threads exit when idle and are replaced under load; each
    # thread's handle must close with it (auto-bkv3p's failure mode).
    import os

    auth_db.init_db(tmp_path / "auth.db")
    auth_db.insert_token(hashlib.sha256(b"t").hexdigest(), "auto-t", "org")

    def open_fds():
        fd_dir = "/proc/self/fd"
        count = 0
        for fd in os.listdir(fd_dir):
            try:
                if os.readlink(os.path.join(fd_dir, fd)).startswith(str(tmp_path / "auth.db")):
                    count += 1
            except OSError:
                pass
        return count

    def one_thread():
        t = threading.Thread(target=auth_db.resolve_token, args=(hashlib.sha256(b"t").hexdigest(),))
        t.start()
        t.join()

    one_thread()  # steady state: the store's own handles are open
    before = open_fds()
    for _ in range(40):
        one_thread()
    # No gc.collect(): thread exit and reference counting alone must close the
    # handle, as under load. These are real descriptors, not Python objects.
    # auth_db's path is module-global, so a background thread another test
    # left running (auto-yzbqq) may open this store too: one connection, two
    # descriptors. The leak guarded here is one connection per exited thread,
    # about 80 descriptors after 40 threads.
    assert open_fds() - before <= 4  # no growth per exited thread
