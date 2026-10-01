"""共识计算与异常值规则测试。"""

import math
import unittest

from src.macro_survey.consensus import RuleSpec, compute_consensus


def sample(anon: str, value: float, weight: float = 1.0, confidence: float = 1.0, seq: int = 1):
    return {"contributor_anon": anon, "value": value, "weight": weight,
            "confidence": confidence, "revision_seq": seq}


class ConsensusTest(unittest.TestCase):
    def test_median_basic(self) -> None:
        rule = RuleSpec(method="median", min_samples=3)
        r = compute_consensus([sample("a", 4.9), sample("b", 5.0), sample("c", 5.1)], rule)
        self.assertTrue(r.sufficient)
        self.assertEqual(r.value, 5.0)
        self.assertEqual(r.usable_samples, 3)

    def test_weighted_mean(self) -> None:
        rule = RuleSpec(method="mean", use_confidence_weight=False, min_samples=2)
        r = compute_consensus([sample("a", 4.0, weight=3), sample("b", 6.0, weight=1)], rule)
        self.assertAlmostEqual(r.value, 4.5)

    def test_confidence_zero_excluded_but_counted(self) -> None:
        rule = RuleSpec(method="median", min_samples=2)
        r = compute_consensus([sample("a", 1.0), sample("b", 2.0), sample("c", 3.0, confidence=0.0)], rule)
        self.assertTrue(r.sufficient)
        self.assertEqual(r.usable_samples, 3)  # 水位内全部样本仍在册
        self.assertEqual(r.included_samples, 2)
        self.assertEqual(r.value, 1.5)  # 等权边界取均值，仅 a,b 进入计算
        self.assertIn("c", r.excluded)

    def test_insufficient_samples(self) -> None:
        rule = RuleSpec(method="median", min_samples=5)
        r = compute_consensus([sample("a", 1), sample("b", 2)], rule)
        self.assertFalse(r.sufficient)
        self.assertIsNone(r.value)

    def test_iqr_outlier_removal(self) -> None:
        rule = RuleSpec.from_dict({
            "method": "median", "min_samples": 4,
            "outlier": {"method": "iqr", "k": 1.5},
        })
        data = [sample("a", 4.9), sample("b", 5.0), sample("c", 4.95),
                sample("d", 5.05), sample("e", 12.0)]
        r = compute_consensus(data, rule)
        self.assertIn("e", r.excluded)
        self.assertEqual(r.usable_samples, 5)  # 样本仍在，只是被剔除
        self.assertTrue(4.9 <= r.value <= 5.05)

    def test_mad_outlier(self) -> None:
        rule = RuleSpec.from_dict({
            "method": "median", "min_samples": 4,
            "outlier": {"method": "mad", "k": 3.5},
        })
        data = [sample("a", 4.95), sample("b", 5.0), sample("c", 5.05),
                sample("d", 5.0), sample("z", 9.9)]
        r = compute_consensus(data, rule)
        self.assertIn("z", r.excluded)

    def test_small_sample_not_flagged(self) -> None:
        rule = RuleSpec.from_dict({"method": "median", "min_samples": 3,
                                   "outlier": {"method": "iqr", "k": 1.5}})
        r = compute_consensus([sample("a", 1), sample("b", 2), sample("c", 100)], rule)
        self.assertEqual(r.excluded, ())

    def test_invalid_params(self) -> None:
        with self.assertRaises(ValueError):
            RuleSpec.from_dict({"method": "mode"})
        with self.assertRaises(ValueError):
            RuleSpec.from_dict({"outlier": {"method": "zscore"}})

    def test_weighted_median(self) -> None:
        rule = RuleSpec(method="median", use_confidence_weight=True, min_samples=2)
        r = compute_consensus([sample("a", 4.0, confidence=0.9),
                               sample("b", 6.0, confidence=0.1)], rule)
        # 累计权重中点落在 a
        self.assertEqual(r.value, 4.0)
        self.assertTrue(math.isfinite(r.detail["spread"]["max"]))


if __name__ == "__main__":
    unittest.main()
