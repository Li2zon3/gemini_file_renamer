# -*- coding: utf-8 -*-
import asyncio
import json
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


_MODULE_PATH = (Path(__file__).resolve().parents[1] / "gemini_file_renamer.py").resolve()

spec = importlib.util.spec_from_file_location("gemini_file_renamer", str(_MODULE_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError(f"Failed to import module from {_MODULE_PATH}")
gfr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gfr
spec.loader.exec_module(gfr)  # type: ignore[union-attr]


class BudgetManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_and_commit_refund(self):
        with tempfile.TemporaryDirectory() as td:
            budget_path = Path(td) / "budget_tracker.json"
            bm = gfr.BudgetManager(budget_path)
            key_id = gfr._make_key_id("dummy-key")

            budget_nanos = 10_000_000_000  # $10
            reservation = await bm.try_reserve(
                key_id=key_id,
                budget_nanos_usd=budget_nanos,
                estimated_input_tokens=1000,
                max_output_tokens=100,
                month="2026-02",
            )
            self.assertIsNotNone(reservation)

            spent_after_reserve = await bm.get_spent_nanos_usd(key_id, month="2026-02")
            self.assertGreater(spent_after_reserve, 0)

            # Actual usage is smaller => should refund part of the reservation.
            await bm.commit(
                reservation=reservation,
                actual_input_tokens=900,
                actual_output_tokens=50,
            )

            spent_after_commit = await bm.get_spent_nanos_usd(key_id, month="2026-02")
            self.assertGreaterEqual(spent_after_commit, 0)
            self.assertLess(spent_after_commit, spent_after_reserve)

            text = budget_path.read_text(encoding="utf-8")
            self.assertIn(key_id, text)
            self.assertNotIn("dummy-key", text)  # raw key must not be persisted

    async def test_budget_exceeded(self):
        with tempfile.TemporaryDirectory() as td:
            budget_path = Path(td) / "budget_tracker.json"
            bm = gfr.BudgetManager(budget_path)
            key_id = gfr._make_key_id("dummy-key")

            reservation = await bm.try_reserve(
                key_id=key_id,
                budget_nanos_usd=1,  # impossible
                estimated_input_tokens=1,
                max_output_tokens=1,
                month="2026-02",
            )
            self.assertIsNone(reservation)

    async def test_rollback_undoes_reservation(self):
        with tempfile.TemporaryDirectory() as td:
            budget_path = Path(td) / "budget_tracker.json"
            bm = gfr.BudgetManager(budget_path)
            key_id = gfr._make_key_id("dummy-key")

            budget_nanos = 10_000_000_000  # $10
            reservation = await bm.try_reserve(
                key_id=key_id,
                budget_nanos_usd=budget_nanos,
                estimated_input_tokens=1000,
                max_output_tokens=100,
                month="2026-02",
            )
            self.assertIsNotNone(reservation)

            spent_after_reserve = await bm.get_spent_nanos_usd(key_id, month="2026-02")
            self.assertGreater(spent_after_reserve, 0)

            await bm.rollback(reservation=reservation)

            spent_after_rollback = await bm.get_spent_nanos_usd(key_id, month="2026-02")
            self.assertLess(spent_after_rollback, spent_after_reserve)

            data = json.loads(budget_path.read_text(encoding="utf-8"))
            entry = data["months"]["2026-02"][key_id]
            self.assertEqual(int(entry.get("requests", 0)), 0)

    async def test_concurrent_reserve_does_not_overshoot_budget(self):
        with tempfile.TemporaryDirectory() as td:
            budget_path = Path(td) / "budget_tracker.json"
            bm = gfr.BudgetManager(budget_path)
            key_id = gfr._make_key_id("dummy-key")

            # Reserve cost ~ 85,000 nanos; budget allows only one.
            budget_nanos = 100_000

            async def reserve_one():
                return await bm.try_reserve(
                    key_id=key_id,
                    budget_nanos_usd=budget_nanos,
                    estimated_input_tokens=100,
                    max_output_tokens=10,
                    month="2026-02",
                )

            r1, r2 = await asyncio.gather(reserve_one(), reserve_one())
            self.assertTrue((r1 is None) ^ (r2 is None))

            spent = await bm.get_spent_nanos_usd(key_id, month="2026-02")
            self.assertLessEqual(spent, budget_nanos)
