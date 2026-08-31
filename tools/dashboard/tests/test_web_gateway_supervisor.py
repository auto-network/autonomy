from __future__ import annotations

import pytest

from tools.dashboard import web_gateway_supervisor as sup


class FakeRuntime:
    def __init__(self):
        self.running = False
        self.healthy = False
        self.starts = 0
        self.stops = 0

    async def ensure_started(self):
        self.starts += 1
        self.running = True
        self.healthy = True

    async def is_healthy(self):
        return self.running and self.healthy

    async def stop(self):
        self.stops += 1
        self.running = False
        self.healthy = False


class FakeLoader:
    def __init__(self):
        self.configs = []
        self.failure = None

    async def __call__(self, config):
        self.configs.append(config)
        if self.failure is not None:
            raise self.failure


def desired(*routes, config="config", ready=True, reason=None):
    return sup.GatewayDesiredState(
        caddyfile=config,
        routes=tuple(sup.DesiredRoute(route_id, fingerprint) for route_id, fingerprint in routes),
        ready=ready,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_empty_desired_state_keeps_gateway_dormant():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)

    result = await supervisor.reconcile(desired())

    assert result["state"] == "stopped"
    assert result["advertised_routes"] == []
    assert runtime.starts == 0
    assert loader.configs == []


@pytest.mark.asyncio
async def test_route_is_advertised_only_after_healthy_atomic_load():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)

    result = await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))

    assert runtime.starts == 1
    assert loader.configs == ["full-v1"]
    assert result["state"] == "healthy"
    assert result["advertised_routes"] == ["r1"]


@pytest.mark.asyncio
async def test_unchanged_full_state_is_a_true_noop():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    plan = desired(("r1", "route-v1"), config="full-v1")
    await supervisor.reconcile(plan)

    result = await supervisor.reconcile(plan)

    assert runtime.starts == 1
    assert loader.configs == ["full-v1"]
    assert result["state"] == "healthy"


@pytest.mark.asyncio
async def test_failed_add_keeps_only_unchanged_last_known_good_routes():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))
    loader.failure = RuntimeError("adapt failed")

    result = await supervisor.reconcile(
        desired(("r1", "route-v1"), ("r2", "route-v1"), config="full-v2")
    )

    assert result["state"] == "failed"
    assert result["advertised_routes"] == ["r1"]
    assert runtime.running is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_plan", "next_plan"),
    [
        (
            desired(("r1", "route-v1"), ("r2", "route-v1"), config="full-v1"),
            desired(("r2", "route-v1"), config="removed-r1"),
        ),
        (
            desired(("r1", "route-v1"), config="full-v1"),
            desired(("r1", "route-v2"), config="retargeted"),
        ),
    ],
)
async def test_failed_remove_or_retarget_stops_stale_authority(initial_plan, next_plan):
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(initial_plan)
    loader.failure = RuntimeError("adapt failed")

    result = await supervisor.reconcile(next_plan)

    assert result["state"] == "failed"
    assert result["advertised_routes"] == []
    assert runtime.running is False
    assert runtime.stops == 1


@pytest.mark.asyncio
async def test_final_route_removal_stops_without_loading_an_empty_config():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1"), config="full-v1"))

    result = await supervisor.reconcile(desired(config="empty"))

    assert result["state"] == "stopped"
    assert result["advertised_routes"] == []
    assert runtime.running is False
    assert runtime.stops == 1
    assert loader.configs == ["full-v1"]


@pytest.mark.asyncio
async def test_unready_dependency_stops_and_never_advertises():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    await supervisor.reconcile(desired(("r1", "route-v1")))

    result = await supervisor.reconcile(
        desired(("r1", "route-v1"), ready=False, reason="connector-unavailable")
    )

    assert result["state"] == "stopped"
    assert result["reason"] == "connector-unavailable"
    assert result["advertised_routes"] == []
    assert runtime.stops == 1


@pytest.mark.asyncio
async def test_dead_gateway_is_restarted_and_complete_state_reloaded():
    runtime = FakeRuntime()
    loader = FakeLoader()
    supervisor = sup.WebGatewaySupervisor(runtime=runtime, loader=loader)
    plan = desired(("r1", "route-v1"), config="full-v1")
    await supervisor.reconcile(plan)
    runtime.running = False
    runtime.healthy = False

    result = await supervisor.reconcile(plan)

    assert runtime.starts == 2
    assert loader.configs == ["full-v1", "full-v1"]
    assert result["state"] == "healthy"
    assert result["advertised_routes"] == ["r1"]


@pytest.mark.asyncio
async def test_identical_failure_backs_off_but_changed_authority_reconciles_now():
    runtime = FakeRuntime()
    loader = FakeLoader()
    loader.failure = RuntimeError("adapt failed")
    clock = [100.0]
    supervisor = sup.WebGatewaySupervisor(
        runtime=runtime, loader=loader, now=lambda: clock[0]
    )
    broken = desired(("r1", "route-v1"), config="broken-v1")

    first = await supervisor.reconcile(broken)
    immediate = await supervisor.reconcile(broken)
    changed = await supervisor.reconcile(
        desired(("r1", "route-v2"), config="broken-v2")
    )

    assert first["state"] == "failed"
    assert immediate["reason"] == "backoff"
    assert len(loader.configs) == 2
    assert changed["state"] == "failed"

    clock[0] += sup.INITIAL_BACKOFF_SECONDS
    await supervisor.reconcile(desired(("r1", "route-v2"), config="broken-v2"))
    assert len(loader.configs) == 3
