"""Stable-owner Web Push subscription and VAPID custody contract."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

from tools.dashboard.dao.web_push import (
    VapidKeyCustody,
    WebPushStore,
    WebPushStoreError,
    _b64url,
)


OWNER_A = "a" * 64
OWNER_B = "b" * 64


def _endpoint(token: str) -> tuple[str, str]:
    value = f"https://web.push.apple.com/Q/{token}"
    return value, hashlib.sha256(value.encode()).hexdigest()


def _subscription(key_id: str, token: str) -> dict:
    endpoint, endpoint_hash = _endpoint(token)
    return {
        "endpoint": endpoint,
        "endpoint_hash": endpoint_hash,
        "endpoint_origin": "https://web.push.apple.com",
        "vapid_subject": "https://dashboard.test",
        "p256dh": _b64url(b"\x04" + b"p" * 64),
        "auth_secret": _b64url(b"a" * 16),
        "vapid_key_id": key_id,
        "expiration_time": None,
    }


@pytest.fixture
def substrate(tmp_path):
    store = WebPushStore(tmp_path / "web-push.db")
    custody = VapidKeyCustody(
        store,
        key_dir=tmp_path / "web-push-keys",
        legacy_key_path=tmp_path / "web-push-vapid.pem",
    )
    active = custody.ensure_active()
    return store, custody, active, tmp_path


class TestWebPushSubscriptionStore:
    def test_schema_is_idempotent_and_defaults_applications_off(self, substrate):
        store, _custody, active, _tmp = substrate
        store.initialize()
        store.initialize()
        assert store.preferences(OWNER_A, ("fleet", "dropbox")) == {
            "fleet": "off",
            "dropbox": "off",
        }
        assert store.set_preference(OWNER_A, "fleet", "generic") == "generic"
        assert store.preferences(OWNER_A, ("fleet", "dropbox")) == {
            "fleet": "generic",
            "dropbox": "off",
        }
        with pytest.raises(WebPushStoreError, match="invalid preference"):
            store.set_preference(OWNER_A, "fleet", "urgent")
        assert len(active.public_key) == 87

    def test_control_labels_and_non_ascii_updater_tokens_fail_closed(self, substrate):
        store, _custody, active, _tmp = substrate
        with pytest.raises(WebPushStoreError, match="invalid device label"):
            store.enroll(
                operator_subject=OWNER_A,
                device_id="device_1234567890",
                device_label="phone\u202ehidden",
                **_subscription(active.key_id, "one"),
            )
        with pytest.raises(WebPushStoreError, match="invalid device token"):
            store.refresh(
                device_id="device_1234567890",
                device_update_token="é" * 43,
                token_version=1,
                old_endpoint_hash="a" * 64,
                subscription=None,
            )

    def test_enrollment_hashes_token_and_refresh_is_single_use_cas(self, substrate):
        store, _custody, active, _tmp = substrate
        first = store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            device_label="Jeremy's phone",
            browser_family="safari",
            platform_family="ios",
            max_detail="generic",
            **_subscription(active.key_id, "one"),
        )
        assert first.token_version == 1
        connection = sqlite3.connect(store.db_path)
        try:
            row = connection.execute(
                "SELECT operator_subject,device_update_token_hash,token_version,"
                "serving_machine_id FROM web_push_subscriptions"
            ).fetchone()
        finally:
            connection.close()
        assert row[0] == OWNER_A
        assert first.device_update_token not in row[1]
        assert row[2:] == (1, None)

        replacement = _subscription(active.key_id, "two")
        second = store.refresh(
            device_id=first.device_id,
            device_update_token=first.device_update_token,
            token_version=first.token_version,
            old_endpoint_hash=_endpoint("one")[1],
            subscription={key: replacement[key] for key in (
                "endpoint", "endpoint_hash", "endpoint_origin", "p256dh",
                "auth_secret", "vapid_key_id", "expiration_time",
            )},
        )
        assert second is not None and second.token_version == 2
        assert second.device_update_token != first.device_update_token
        with pytest.raises(WebPushStoreError, match="device not found"):
            store.refresh(
                device_id=first.device_id,
                device_update_token=first.device_update_token,
                token_version=first.token_version,
                old_endpoint_hash=_endpoint("one")[1],
                subscription=None,
            )

    def test_concurrent_refresh_has_exactly_one_winner(self, substrate):
        store, _custody, active, _tmp = substrate
        first = store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            **_subscription(active.key_id, "initial"),
        )

        def refresh(token: str):
            replacement = _subscription(active.key_id, token)
            try:
                return store.refresh(
                    device_id=first.device_id,
                    device_update_token=first.device_update_token,
                    token_version=first.token_version,
                    old_endpoint_hash=_endpoint("initial")[1],
                    subscription={key: replacement[key] for key in (
                        "endpoint", "endpoint_hash", "endpoint_origin", "p256dh",
                        "auth_secret", "vapid_key_id", "expiration_time",
                    )},
                )
            except WebPushStoreError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(refresh, ("winner-a", "winner-b")))
        assert sum(not isinstance(item, str) for item in outcomes) == 1
        assert outcomes.count("device_not_found") == 1

    def test_device_and_endpoint_collisions_fail_closed(self, substrate):
        store, _custody, active, _tmp = substrate
        store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            **_subscription(active.key_id, "same"),
        )
        with pytest.raises(WebPushStoreError, match="device conflict"):
            store.enroll(
                operator_subject=OWNER_B,
                device_id="device_1234567890",
                **_subscription(active.key_id, "other"),
            )
        with pytest.raises(WebPushStoreError, match="endpoint conflict"):
            store.enroll(
                operator_subject=OWNER_B,
                device_id="device_abcdefghij",
                **_subscription(active.key_id, "same"),
            )

    def test_refresh_can_only_replace_transport_fields(self, substrate):
        store, _custody, active, _tmp = substrate
        first = store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            max_detail="descriptive",
            device_label="Private phone",
            **_subscription(active.key_id, "before"),
        )
        store.set_preference(OWNER_A, "fleet", "descriptive")
        replacement = _subscription(active.key_id, "after")
        result = store.refresh(
            device_id=first.device_id,
            device_update_token=first.device_update_token,
            token_version=first.token_version,
            old_endpoint_hash=_endpoint("before")[1],
            subscription={key: replacement[key] for key in (
                "endpoint", "endpoint_hash", "endpoint_origin", "p256dh",
                "auth_secret", "vapid_key_id", "expiration_time",
            )},
        )
        assert result is not None
        listed = store.list_devices(OWNER_A)
        assert listed[0]["max_detail"] == "descriptive"
        assert listed[0]["device_label"] == "Private phone"
        assert store.preferences(OWNER_A, ("fleet",)) == {"fleet": "descriptive"}

    def test_token_authenticated_null_refresh_retires_and_cancels(self, substrate):
        store, _custody, active, _tmp = substrate
        first = store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            **_subscription(active.key_id, "gone"),
        )
        retired = store.refresh(
            device_id=first.device_id,
            device_update_token=first.device_update_token,
            token_version=first.token_version,
            old_endpoint_hash=_endpoint("gone")[1],
            subscription=None,
        )
        assert retired is None
        assert store.state(OWNER_A, first.device_id)["this_installation"]["status"] == "retired"

    def test_vapid_rotation_preserves_old_key_until_subscriptions_move(self, substrate):
        store, custody, old, _tmp = substrate
        store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            **_subscription(old.key_id, "old"),
        )
        new = custody.rotate(retire_after=10**10)
        assert custody.ensure_active().key_id == new.key_id
        assert custody.load(old.key_id)[0].status == "retiring"
        with pytest.raises(WebPushStoreError, match="still in use"):
            custody.retire(old.key_id)

        store.enroll(
            operator_subject=OWNER_A,
            device_id="device_1234567890",
            **_subscription(new.key_id, "new"),
        )
        custody.retire(old.key_id)
        with pytest.raises(WebPushStoreError, match="unavailable"):
            custody.load(old.key_id)

    def test_key_directory_and_files_have_exact_modes(self, substrate):
        _store, _custody, active, tmp = substrate
        directory = tmp / "web-push-keys"
        key_file = directory / active.private_key_path
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
        assert key_file.parent == directory

    def test_concurrent_key_generation_converges(self, tmp_path):
        store = WebPushStore(tmp_path / "web-push.db")

        def initialize(_index: int):
            return VapidKeyCustody(
                store,
                key_dir=tmp_path / "web-push-keys",
                legacy_key_path=tmp_path / "legacy.pem",
            ).ensure_active()

        with ThreadPoolExecutor(max_workers=2) as executor:
            records = list(executor.map(initialize, (1, 2)))
        assert records[0].key_id == records[1].key_id
        assert records[0].public_key == records[1].public_key
        assert len(list((tmp_path / "web-push-keys").glob("*.pem"))) == 1

    def test_key_metadata_path_public_key_and_file_mode_fail_closed(self, substrate):
        store, custody, active, tmp = substrate
        connection = sqlite3.connect(store.db_path)
        connection.execute(
            "UPDATE web_push_vapid_keys SET private_key_path='../escape.pem' WHERE key_id=?",
            (active.key_id,),
        )
        connection.commit()
        connection.close()
        with pytest.raises(WebPushStoreError, match="path invalid"):
            custody.ensure_active()

        connection = sqlite3.connect(store.db_path)
        connection.execute(
            "UPDATE web_push_vapid_keys SET private_key_path=? WHERE key_id=?",
            (active.private_key_path, active.key_id),
        )
        connection.commit()
        connection.close()
        key_file = tmp / "web-push-keys" / active.private_key_path
        os.chmod(key_file, 0o644)
        with pytest.raises(WebPushStoreError, match="mode mismatch"):
            custody.ensure_active()
        os.chmod(key_file, 0o600)

        connection = sqlite3.connect(store.db_path)
        connection.execute(
            "UPDATE web_push_vapid_keys SET public_key=? WHERE key_id=?",
            ("A" * 87, active.key_id),
        )
        connection.commit()
        connection.close()
        with pytest.raises(WebPushStoreError, match="key material mismatch"):
            custody.ensure_active()

    def test_future_schema_version_fails_closed(self, tmp_path):
        db = tmp_path / "future.db"
        connection = sqlite3.connect(db)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        with pytest.raises(WebPushStoreError, match="unsupported schema"):
            WebPushStore(db).initialize()

    def test_partial_current_subscription_schema_fails_closed(self, tmp_path):
        db = tmp_path / "partial.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "CREATE TABLE web_push_subscriptions("
            "id TEXT PRIMARY KEY,operator_subject TEXT,device_id TEXT)"
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        with pytest.raises(WebPushStoreError, match="unsupported subscription schema"):
            WebPushStore(db).initialize()

    def test_legacy_subscription_and_vapid_key_migrate_without_rotation(self, tmp_path):
        legacy_key = tmp_path / "web-push-vapid.pem"
        private_key = ec.generate_private_key(ec.SECP256R1())
        legacy_key.write_bytes(private_key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
        ))
        os.chmod(legacy_key, 0o600)
        expected_public = _b64url(private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        ))
        db = tmp_path / "web-push.db"
        connection = sqlite3.connect(db)
        connection.executescript("""
            CREATE TABLE web_push_subscriptions (
                installation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                origin TEXT NOT NULL, endpoint TEXT NOT NULL UNIQUE,
                endpoint_hash TEXT NOT NULL, p256dh TEXT NOT NULL,
                auth_secret TEXT NOT NULL, vapid_key_id TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL,
                last_seen_at REAL NOT NULL, retired_at REAL, retire_reason TEXT
            );
        """)
        endpoint, endpoint_hash = _endpoint("legacy")
        connection.execute(
            "INSERT INTO web_push_subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "device_1234567890", OWNER_A, "https://dashboard.test", endpoint,
                endpoint_hash, "p", "a", "primary", "active", 1.0, 2.0, None, None,
            ),
        )
        connection.commit()
        connection.close()

        store = WebPushStore(db)
        custody = VapidKeyCustody(
            store, key_dir=tmp_path / "web-push-keys", legacy_key_path=legacy_key,
        )
        active = custody.ensure_active()
        assert active.public_key == expected_public
        connection = sqlite3.connect(db)
        try:
            row = connection.execute(
                "SELECT operator_subject,device_id,vapid_key_id,vapid_subject "
                "FROM web_push_subscriptions"
            ).fetchone()
        finally:
            connection.close()
        assert row == (OWNER_A, "device_1234567890", active.key_id, "https://dashboard.test")
