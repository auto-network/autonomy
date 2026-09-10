"""A telemetry write that fails must say so — quietly, but once."""
import logging
import pytest
from tools.network import fleet_sync_scheduler as fss


@pytest.fixture(autouse=True)
def _clear():
    fss._TELEMETRY_FAILURE_LOGGED.clear()
    yield
    fss._TELEMETRY_FAILURE_LOGGED.clear()


def test_a_failed_write_is_reported(caplog):
    with caplog.at_level(logging.WARNING, logger=fss.logger.name):
        fss._log_telemetry_failure("serve", "autonomy", ValueError("undeclared field"))
    assert "NOT being recorded" in caplog.text
    assert "autonomy" in caplog.text
    assert "undeclared field" in caplog.text


def test_a_persistent_failure_does_not_become_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger=fss.logger.name):
        for _ in range(50):
            fss._log_telemetry_failure("serve", "autonomy", ValueError("x"))
    assert caplog.text.count("NOT being recorded") == 1


def test_each_scope_reports_on_its_own(caplog):
    with caplog.at_level(logging.WARNING, logger=fss.logger.name):
        fss._log_telemetry_failure("serve", "autonomy", ValueError("x"))
        fss._log_telemetry_failure("serve", "anchore", ValueError("x"))
        fss._log_telemetry_failure("after-serve", "autonomy", ValueError("x"))
    assert caplog.text.count("NOT being recorded") == 3
