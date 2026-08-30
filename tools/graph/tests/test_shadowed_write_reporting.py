"""A write that changes nothing anyone reads must say so.

The condition is real and silent: the row is stored, the id comes back, the
command prints success, and every reader still sees the old value. Nothing
about that sequence looks wrong from the caller's side, so the only thing
standing between it and hours of confusion is the report.

The protection existed and never fired once. It had no test at all, and two
defects that a test of the real path would have caught immediately:

* it looked only at other BASE rows, so an override masking the write -- the
  commonest way a write is neutralised -- was invisible to it;
* it was reported from a module-global that only an in-process write
  populates, while the write happens in the dashboard whenever ``GRAPH_API``
  is set. The server detected the condition and returned it; the client threw
  the response away and consulted its own empty dict.

So these tests cover the DETECTION and the DELIVERY separately, because the
feature was broken in both and either alone is enough to make it useless.
"""
from __future__ import annotations

import pytest

from tools.graph import set_cmd, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)


@pytest.fixture(scope="module")
def probe_schema():
    @keyed_per_entity(key_strategy="probe_id")
    class Probe(SettingSchema):
        set_id = "probe.shadow.value"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @keyed_per_entity(key_strategy="probe_id")
    class Wide(SettingSchema):
        set_id = "probe.shadow.wide"
        schema_revision = 1
        a: str = field(required=True, description="a")
        b: str = field(required=True, description="b")

    @keyed_per_entity(key_strategy="probe_id")
    class Probe2(Probe):
        set_id = "probe.shadow.value"
        schema_revision = 2

        @classmethod
        def upconvert_from_prev(cls, payload):
            return dict(payload)

    return Probe, Wide


@pytest.fixture
def orgs(tmp_path, monkeypatch, probe_schema):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


# ── detection ────────────────────────────────────────────────


def test_a_write_nothing_reads_is_reported(orgs):
    """The case it was built for: a peer publishes, so raw loses."""
    settings_ops.upsert_by_key("probe.shadow.value", 1, "k", {"v": "theirs"},
                               org="partner", state="published")
    settings_ops.add_setting("probe.shadow.value", 1, "k", {"v": "mine"},
                             org="acme", state="raw")

    shadow = settings_ops.take_shadowed_write("probe.shadow.value", "k")

    assert shadow is not None, (
        "a stored row that resolution will never return was written and "
        "nothing said so")
    assert "partner" in str(shadow)


def test_a_write_that_wins_is_not_reported(orgs):
    """The check has to discriminate, or it is noise that gets ignored."""
    settings_ops.add_setting("probe.shadow.value", 1, "solo", {"v": "mine"},
                             org="acme", state="raw")

    assert settings_ops.take_shadowed_write("probe.shadow.value", "solo") is None


def test_the_report_is_taken_once(orgs):
    """It is consumed by the command that wrote it; a second reader getting
    a stale warning would attach it to the wrong write."""
    settings_ops.upsert_by_key("probe.shadow.value", 1, "once", {"v": "t"},
                               org="partner", state="published")
    settings_ops.add_setting("probe.shadow.value", 1, "once", {"v": "m"},
                             org="acme", state="raw")

    assert settings_ops.take_shadowed_write("probe.shadow.value", "once")
    assert settings_ops.take_shadowed_write("probe.shadow.value", "once") is None


def test_a_revision_migration_write_is_not_reported_backwards(orgs):
    """The normal transient state of a revision migration: one key holds a
    rev-1 and a rev-2 base at the same rung. The resolver is revision-aware
    (higher revision wins), so the freshly migrated rev-2 row is the row
    everything reads — reporting it as shadowed by the rev-1 row it just
    replaced is the check disagreeing with the thing it checks (host
    finding, 2026-08-30), and its advice ('remove the row') then deletes
    the migrated row."""
    settings_ops.add_setting("probe.shadow.value", 1, "mig", {"v": "old"},
                             org="acme", state="raw")
    settings_ops.add_setting("probe.shadow.value", 2, "mig", {"v": "new"},
                             org="acme", state="raw")

    assert settings_ops.take_shadowed_write("probe.shadow.value", "mig") is None


def test_writing_the_old_revision_under_a_newer_one_is_reported(orgs):
    """The mirror case is a real shadow: a rev-1 write under an existing
    rev-2 base loses to it, exactly as the resolver ranks."""
    settings_ops.add_setting("probe.shadow.value", 2, "back", {"v": "new"},
                             org="acme", state="raw")
    settings_ops.add_setting("probe.shadow.value", 1, "back", {"v": "old"},
                             org="acme", state="raw")

    shadow = settings_ops.take_shadowed_write("probe.shadow.value", "back")
    assert shadow is not None


def test_remove_by_key_refuses_an_ambiguous_key(orgs, capsys):
    """`set remove <set_id> <key>` with two live bases under the key must
    refuse and name ids — resolution picks the migration's NEW row, so
    'remove by key' during a migration silently deletes the row just
    migrated to (host finding, 2026-08-30)."""
    from types import SimpleNamespace

    settings_ops.add_setting("probe.shadow.value", 1, "amb", {"v": "old"},
                             org="acme", state="raw")
    settings_ops.add_setting("probe.shadow.value", 2, "amb", {"v": "new"},
                             org="acme", state="raw")

    args = SimpleNamespace(id_parts=["probe.shadow.value", "amb"], org="acme")
    with pytest.raises(SystemExit):
        set_cmd.cmd_set_remove(args)
    err = capsys.readouterr().err
    assert "2 live base row(s)" in err
    assert "rev=1" in err and "rev=2" in err


# ── delivery ─────────────────────────────────────────────────


class _ClientWithReport:
    """Stands in for the HTTP client after a write the server flagged."""

    def __init__(self, report):
        self.last_write_report = report


def test_the_warning_is_read_off_the_write_response(capsys):
    """Where the write happens in another process, this is the only source.

    The server puts ``shadowed_by`` in the response body. Reading it there
    is what makes the warning work for a container session -- every one of
    which routes writes through the dashboard.
    """
    client = _ClientWithReport({
        "id": "abc",
        "shadowed_by": {"message": "written row is shadowed by partner's canonical row"},
    })

    set_cmd._report_shadowed_write("probe.shadow.value", "k", client)

    out = capsys.readouterr().out
    assert "shadowed by partner" in out
    assert "***" in out, "a warning that does not stand out is not a warning"


def test_a_clean_write_prints_nothing(capsys):
    set_cmd._report_shadowed_write(
        "probe.shadow.value", "k", _ClientWithReport({"id": "abc"}))

    assert capsys.readouterr().out == ""


def test_no_client_falls_back_to_the_in_process_report(orgs, capsys):
    """``--force-host`` really does write here, and has no response to read."""
    settings_ops.upsert_by_key("probe.shadow.value", 1, "fb", {"v": "t"},
                               org="partner", state="published")
    settings_ops.add_setting("probe.shadow.value", 1, "fb", {"v": "m"},
                             org="acme", state="raw")

    set_cmd._report_shadowed_write("probe.shadow.value", "fb", None)

    assert "partner" in capsys.readouterr().out


def test_a_client_that_never_wrote_does_not_crash_the_report(capsys):
    """The ops module is passed as a client under --force-host and has no
    such attribute; a reporter that raised here would break the write path
    it exists to annotate."""
    set_cmd._report_shadowed_write("probe.shadow.value", "absent", object())

    assert capsys.readouterr().out == ""


# ── masking, the case it could not see ───────────────────────


def test_an_override_that_overwrites_the_write_is_reported(orgs, monkeypatch):
    """Winning the base contest is not the same as being read.

    This is how a write is most often neutralised, and the original check
    was blind to it by construction: its query excluded override rows, so
    the row that actually decides the value was the one row it never
    looked at.
    """
    base = settings_ops.add_setting("probe.shadow.value", 1, "m", {"v": "old"},
                                    org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "override wins"}, org="acme")

    settings_ops.upsert_by_key("probe.shadow.value", 1, "m", {"v": "new"},
                               org="acme")
    shadow = settings_ops.take_shadowed_write("probe.shadow.value", "m")

    assert shadow is not None, (
        "the write stored a value no reader will see and said nothing")
    assert shadow.masked_fields == ("v",)
    assert "MASKED" in str(shadow)


def test_a_write_an_override_does_not_touch_is_not_reported(orgs, monkeypatch):
    """Only the fields actually overwritten count. An override on another
    field leaves this write fully effective, and warning about it would
    train the reader to ignore the warning."""
    base = settings_ops.add_setting(
        "probe.shadow.wide", 1, "w", {"a": "1", "b": "2"}, org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"b": "override"}, org="acme")

    settings_ops.upsert_by_key("probe.shadow.wide", 1, "w",
                               {"a": "changed", "b": "override"}, org="acme")

    assert settings_ops.take_shadowed_write("probe.shadow.wide", "w") is None


# ── what readers will see, said every time ───────────────────


def test_a_write_says_what_the_value_now_is(orgs, capsys):
    """"Written" and "this is the value" are different facts."""
    settings_ops.add_setting("probe.shadow.value", 1, "eff", {"v": "mine"},
                             org="acme")

    set_cmd._report_effective_value(
        "probe.shadow.value", "eff", "acme", {"v": "mine"})

    assert "exactly what you wrote" in capsys.readouterr().out


def test_it_names_the_fields_that_did_not_take(orgs, capsys):
    """The case that cost a day: the row lands, and a reader sees something
    else. Naming the field is the difference between a report and a hint."""
    settings_ops.upsert_by_key("probe.shadow.value", 1, "eff2", {"v": "theirs"},
                               org="partner", state="published")

    set_cmd._report_effective_value(
        "probe.shadow.value", "eff2", "acme", {"v": "mine"})

    out = capsys.readouterr().out
    assert "differs from what you wrote in v" in out
    assert "theirs" in out, "the value a reader gets has to be shown, not implied"


def test_a_key_that_resolves_to_nothing_says_so(orgs, capsys):
    """Distinct from agreeing. A write that resolves to nothing is the
    loudest possible version of the failure and printed nothing before."""
    set_cmd._report_effective_value(
        "probe.shadow.value", "absent", "acme", {"v": "mine"})

    assert "does not resolve" in capsys.readouterr().out


def test_it_is_printed_even_when_nothing_is_wrong(orgs, capsys):
    """A report that appears only on trouble teaches that silence means
    agreement -- and silence is what the failing case produced."""
    settings_ops.add_setting("probe.shadow.value", 1, "quiet", {"v": "x"},
                             org="acme")

    set_cmd._report_effective_value(
        "probe.shadow.value", "quiet", "acme", {"v": "x"})

    assert capsys.readouterr().out.strip(), "a silent success is the old behaviour"
