"""auto-mmwgu observability: what a process holds of org sync certificates
and channels is reported, never inferred."""
from tools.dashboard import org_sync_channels


def test_report_names_certificate_key_and_channel_per_slug() -> None:
    cert = {"v": 1, "child_pub": "aa" * 32, "scope": ["fleet:sync"], "org": "g" * 64,
            "subject": {"kind": "persona", "id": "bb" * 32},
            "not_before": 1, "not_after": 2_000_000_000, "sig": "cc" * 64}
    org_sync_channels.install({"acme": cert}, {"acme": object(), "orphan-key": object()})
    try:
        report = org_sync_channels.report()
        assert report["acme"]["certificate"] == {
            "child_pub": "aa" * 32, "persona": "bb" * 32, "org": "g" * 64, "not_after": 2_000_000_000,
        }
        assert report["acme"]["key_held"] is True and report["acme"]["channel"] is False
        assert report["orphan-key"] == {"certificate": None, "key_held": True, "channel": False}
        org_sync_channels.install({}, {})
        assert org_sync_channels.report() == {}
    finally:
        org_sync_channels.install({}, {})
