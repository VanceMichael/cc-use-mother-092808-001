"""共识统计：中位数、加权均值与 IQR 异常值规则。纯函数，不触碰存储。"""

from __future__ import annotations

from typing import Any

from .models import DomainError, RuleDefinition

# 样本少于此数时 IQR 围栏不稳定，跳过异常值剔除
IQR_MIN_SAMPLES = 4
# 精度加权中避免除以零的平滑项
ACCURACY_EPSILON = 0.01


def median(values: list[float]) -> float:
    if not values:
        raise ValueError("空序列没有中位数")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _percentile(ordered: list[float], p: float) -> float:
    """线性插值分位数，ordered 必须已排序。"""
    if len(ordered) == 1:
        return ordered[0]
    rank = p * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def iqr_fences(values: list[float], k: float) -> tuple[float, float]:
    ordered = sorted(values)
    q1 = _percentile(ordered, 0.25)
    q3 = _percentile(ordered, 0.75)
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr


def compute_consensus(
    samples: list[dict[str, Any]],
    rule: RuleDefinition,
    accuracy_mae: dict[str, float] | None = None,
) -> dict[str, Any]:
    """对候选样本应用规则，返回聚合值与逐样本标记。

    samples 的每一项至少包含 key（内部身份，用于精度加权）、value、confidence。
    返回的 samples 保留输入顺序，并补充 outlier / included / weight 字段。
    """
    if not samples:
        raise DomainError("没有可用于共识计算的样本", code="consensus_empty")
    accuracy_mae = accuracy_mae or {}
    values = [s["value"] for s in samples]

    fences: tuple[float, float] | None = None
    if rule.outlier_method == "iqr" and len(values) >= IQR_MIN_SAMPLES:
        fences = iqr_fences(values, rule.outlier_k)

    enriched: list[dict[str, Any]] = []
    for sample in samples:
        outlier = fences is not None and (
            sample["value"] < fences[0] or sample["value"] > fences[1]
        )
        enriched.append({**sample, "outlier": outlier, "included": not outlier})

    included = [s for s in enriched if s["included"]]
    if not included:
        raise DomainError("异常值规则排除了全部样本", code="consensus_empty")

    weights = _weights(included, rule, accuracy_mae)
    total = sum(weights)
    for sample, weight in zip(included, weights):
        sample["weight"] = weight / total
    for sample in enriched:
        sample.setdefault("weight", 0.0)

    return {
        "median": median([s["value"] for s in included]),
        "weighted_mean": sum(s["value"] * s["weight"] for s in included),
        "n_candidates": len(samples),
        "n_included": len(included),
        "n_outliers": sum(1 for s in enriched if s["outlier"]),
        "fences": list(fences) if fences else None,
        "samples": enriched,
    }


def _weights(
    included: list[dict[str, Any]],
    rule: RuleDefinition,
    accuracy_mae: dict[str, float],
) -> list[float]:
    if rule.weighting == "confidence":
        weights = [max(float(s.get("confidence") or 0.0), 0.0) for s in included]
        if sum(weights) > 0:
            return weights
        return [1.0] * len(included)
    if rule.weighting == "accuracy":
        known = [accuracy_mae[s["key"]] for s in included if s["key"] in accuracy_mae]
        if not known:
            return [1.0] * len(included)
        fallback = sum(known) / len(known)
        return [1.0 / (accuracy_mae.get(s["key"], fallback) + ACCURACY_EPSILON) for s in included]
    return [1.0] * len(included)
