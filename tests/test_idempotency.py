"""回执幂等与隔离行为。"""

from __future__ import annotations

import unittest

from src.forecast_backend import DomainError
from tests.forecast_case import ForecastCase


class IdempotencyTest(ForecastCase):
    def test_same_receipt_same_payload_returns_original(self) -> None:
        first = self.submit("bank-a", 4.8, receipt_id="rcpt-1")
        replay = self.submit("bank-a", 4.8, receipt_id="rcpt-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["submission_id"], first["submission_id"])
        history = self.system.revision_history(
            self.auditor,
            institution_id="bank-a",
            indicator="IPCA",
            target_year=2026,
            round_id="R1",
        )
        self.assertEqual(len(history), 1, "重复回执不得产生第二条提交")

    def test_same_receipt_different_payload_is_quarantined(self) -> None:
        self.submit("bank-a", 4.8, receipt_id="rcpt-2")
        with self.assertRaisesRegex(DomainError, "隔离") as caught:
            self.submit("bank-a", 5.2, receipt_id="rcpt-2")
        self.assertEqual(caught.exception.code, "receipt_conflict")

        quarantine = self.system.list_quarantine(self.auditor)
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0]["receipt_id"], "rcpt-2")
        self.assertEqual(quarantine[0]["payload"]["value"], 5.2)

        # 被隔离的到达不产生提交
        history = self.system.revision_history(
            self.auditor,
            institution_id="bank-a",
            indicator="IPCA",
            target_year=2026,
            round_id="R1",
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["value"], 4.8)

    def test_quarantine_survives_restart(self) -> None:
        self.submit("bank-a", 4.8, receipt_id="rcpt-3")
        with self.assertRaises(DomainError):
            self.submit("bank-a", 5.0, receipt_id="rcpt-3")
        self.system.close()
        from src.forecast_backend import ForecastSystem

        self.system = ForecastSystem(self.db_path, clock=self.clock)
        self.assertEqual(len(self.system.list_quarantine(self.auditor)), 1)


if __name__ == "__main__":
    unittest.main()
