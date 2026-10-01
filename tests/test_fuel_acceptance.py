from __future__ import annotations

import unittest
from pathlib import Path

from fuel_cycle.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class FuelAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["duplicate_import_blocked"])
        self.assertTrue(result["quarantine_survives_replay"])
        self.assertTrue(result["replay_response_reused"])
        self.assertEqual(result["declaration_versions"], 2)
        self.assertEqual(result["impact_affected_after_correction"], 2)
        self.assertEqual(result["impact_irreversible"], 1)
        self.assertEqual(result["recovered_batch"], "released")
        self.assertTrue(result["chain_valid"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")


if __name__ == "__main__":
    unittest.main()
