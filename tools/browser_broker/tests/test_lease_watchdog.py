"""auto-8q7oe.5: the watchdog ends the lease at the expiry in force."""

from tools.browser_broker.lease_watchdog import current_expiry, parse_expiry, wait_for_expiry


class Clock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 1.0
        self.sleeps.append(seconds)
        self.now += seconds


def test_parse_expiry():
    assert parse_expiry("1790000000") == 1790000000.0
    assert parse_expiry(" 12.5\n") == 12.5
    for bad in (None, "", "soon", "-5", "0", "nan", "inf"):
        assert parse_expiry(bad) is None


def test_the_newest_expiry_wins_over_the_launch_value():
    assert current_expiry("100", None) == 100.0
    assert current_expiry("100", "250") == 250.0
    assert current_expiry("100", "50") == 50.0     # /expiry may also shorten the lease
    assert current_expiry("100", "garbage") == 100.0


def test_returns_at_the_launch_expiry_within_a_poll():
    clock = Clock(1000.0)
    assert wait_for_expiry("1010", lambda: None, clock.time, clock.sleep) == 1010.0
    assert 1010.0 <= clock.now <= 1011.0


def test_an_extension_read_mid_wait_is_honoured():
    clock = Clock(1000.0)
    extended = {"value": None}

    def read():
        if clock.now >= 1005:
            extended["value"] = "1020"
        return extended["value"]

    assert wait_for_expiry("1010", read, clock.time, clock.sleep) == 1020.0
    assert 1020.0 <= clock.now <= 1021.0


def test_no_valid_expiry_ends_the_lease_at_once():
    clock = Clock(1000.0)
    wait_for_expiry(None, lambda: "nonsense", clock.time, clock.sleep)
    assert clock.sleeps == []


def test_a_past_expiry_ends_the_lease_at_once():
    clock = Clock(1000.0)
    wait_for_expiry("900", lambda: None, clock.time, clock.sleep)
    assert clock.sleeps == []
