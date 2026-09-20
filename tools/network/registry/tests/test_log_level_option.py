"""auto-0tfuz: the registry's own logger can be raised so routing lines
reach the log, without touching uvicorn's token-bearing loggers."""
import io
import logging

from tools.network.registry.__main__ import REGISTRY_LOGGER, configure_registry_logging


def test_the_registry_logger_is_raised_once_and_kept_off_the_root() -> None:
    stream = io.StringIO()
    logger = configure_registry_logging("info", stream=stream)
    configure_registry_logging("info", stream=stream)          # idempotent
    assert logger.name == REGISTRY_LOGGER and logger.level == logging.INFO
    assert sum(getattr(h, "_registry_handler", False) for h in logger.handlers) == 1
    assert logger.propagate is False
    logging.getLogger("tools.network.registry.relay").info("relay dial routed token=abc")
    assert "relay dial routed" in stream.getvalue()
    assert logging.getLogger("uvicorn.error").level in (logging.NOTSET, logging.WARNING)
    configure_registry_logging("warning", stream=stream)
    assert logger.level == logging.WARNING
