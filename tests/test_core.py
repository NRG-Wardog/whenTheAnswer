from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from watcher import reliability as reliability_module
from watcher.errors import CircuitOpen, ProtectionEvent
from watcher.reliability import (
    AuthenticationBudget,
    ProtectionController,
    RequestGate,
)
from watcher.utils import normalize_text, parse_retry_after, utc_iso, utc_now


class UtilityTests(unittest.TestCase):
    def test_normalize_text_collapses_whitespace_and_direction_marks(self) -> None:
        self.assertEqual(normalize_text("  hello\u200f   world  "), "hello world")

    def test_retry_after_parses_seconds(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120)

    def test_retry_after_parses_http_date(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        future = now + timedelta(seconds=90)
        value = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(parse_retry_after(value, now=now), 90)

    def test_retry_after_rejects_invalid_value(self) -> None:
        self.assertIsNone(parse_retry_after("not-a-date"))


class ProtectionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "protection.json"
        self.controller = ProtectionController(self.state_path)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_transient_failures_open_and_persist_circuit(self) -> None:
        with mock.patch.object(
            reliability_module,
            "jittered_seconds",
            side_effect=lambda value, _ratio: float(value),
        ):
            self.assertEqual(await self.controller.record_transient_failure("one"), 0)
            self.assertEqual(await self.controller.record_transient_failure("two"), 0)
            cooldown = await self.controller.record_transient_failure("three", status=503)

        self.assertGreaterEqual(cooldown, 60)
        self.assertEqual(self.controller.state.circuit_state, "OPEN")
        self.assertEqual(self.controller.state.last_status, 503)
        self.assertTrue(self.state_path.exists())

        reloaded = ProtectionController(self.state_path)
        self.assertEqual(reloaded.state.circuit_state, "OPEN")
        self.assertEqual(reloaded.state.last_reason, "three")

    async def test_protection_event_uses_retry_after_and_records_reason(self) -> None:
        event = ProtectionEvent(
            "rate limited",
            kind="rate_limit",
            status=429,
            retry_after_seconds=180,
        )
        with mock.patch.object(
            reliability_module,
            "jittered_seconds",
            side_effect=lambda value, _ratio: float(value),
        ):
            cooldown = await self.controller.record_protection_event(event)

        self.assertEqual(cooldown, 180)
        self.assertEqual(self.controller.state.circuit_state, "OPEN")
        self.assertEqual(self.controller.state.last_status, 429)
        self.assertEqual(self.controller.state.last_reason, "rate limited")

    async def test_success_resets_open_circuit(self) -> None:
        event = ProtectionEvent(
            "blocked",
            kind="block",
            status=403,
            retry_after_seconds=60,
        )
        with mock.patch.object(
            reliability_module,
            "jittered_seconds",
            side_effect=lambda value, _ratio: float(value),
        ):
            await self.controller.record_protection_event(event)

        await self.controller.record_success()

        self.assertEqual(self.controller.state.circuit_state, "CLOSED")
        self.assertEqual(self.controller.state.consecutive_failures, 0)
        self.assertIsNone(self.controller.state.cooldown_until)
        self.assertEqual(self.controller.state.last_reason, "")

    async def test_prepare_activity_rejects_work_while_circuit_is_open(self) -> None:
        self.controller.state.circuit_state = "OPEN"
        self.controller.state.last_reason = "test block"
        self.controller.state.cooldown_until = utc_iso(
            utc_now() + timedelta(minutes=5)
        )
        self.controller.save()

        with self.assertRaises(CircuitOpen):
            await self.controller.prepare_activity(allow_recovery_probe=False)

    async def test_expired_open_circuit_enters_half_open_for_recovery(self) -> None:
        self.controller.state.circuit_state = "OPEN"
        self.controller.state.last_reason = "cooldown complete"
        self.controller.state.cooldown_until = utc_iso(
            utc_now() - timedelta(seconds=1)
        )
        self.controller.save()

        await self.controller.prepare_activity(allow_recovery_probe=True)

        self.assertEqual(self.controller.state.circuit_state, "HALF_OPEN")
        with self.assertRaises(CircuitOpen):
            await self.controller.prepare_activity(allow_recovery_probe=False)

    async def test_protection_notifications_are_throttled(self) -> None:
        self.assertTrue(await self.controller.should_notify())
        self.assertFalse(await self.controller.should_notify())


class AuthenticationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reauthentication_too_soon_is_rejected(self) -> None:
        budget = AuthenticationBudget()
        budget.record_or_raise(initial_login=True)

        with self.assertRaises(ProtectionEvent) as caught:
            budget.record_or_raise(initial_login=False)

        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertIsNotNone(caught.exception.retry_after_seconds)
        self.assertGreater(caught.exception.retry_after_seconds, 0)

    async def test_hourly_authentication_budget_is_enforced(self) -> None:
        budget = AuthenticationBudget()
        budget.record_or_raise(initial_login=True)
        budget.record_or_raise(initial_login=True)

        with self.assertRaises(ProtectionEvent) as caught:
            budget.record_or_raise(initial_login=True)

        self.assertEqual(caught.exception.kind, "rate_limit")
        self.assertEqual(caught.exception.retry_after_seconds, 60 * 60)


class RequestGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_serializes_concurrent_top_level_operations(self) -> None:
        gate = RequestGate(minimum_gap_seconds=0, maximum_requests_per_hour=10)
        active = 0
        max_active = 0
        entered: list[str] = []

        async def worker(label: str) -> None:
            nonlocal active, max_active
            async with gate.slot(label):
                active += 1
                max_active = max(max_active, active)
                entered.append(label)
                await asyncio.sleep(0)
                active -= 1

        await asyncio.gather(worker("first"), worker("second"))

        self.assertEqual(max_active, 1)
        self.assertCountEqual(entered, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
