"""共识计算与异常值规则（纯函数，便于回测复用）。

样本条目约定为 dict，至少包含：
    value: float
    weight: float（>0）
    confidence: 0..1（可选，缺省 1.0）

规则参数 RuleSpec：
    method: "mean" | "median"
    use_confidence_weight: 置信度是否乘入权重
    outlier: None | {"method": "iqr", "k": 1.5} | {"method": "mad", "k": 3.5}
    min_samples: 有效样本下限
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class OutlierSpec:
    method: str  # "iqr" | "mad"
    k: float = 1.5

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "OutlierSpec | None":
        if raw is None:
            return None
        method = raw.get("method")
        if method not in ("iqr", "mad"):
            raise ValueError("异常值方法仅支持 iqr 或 mad")
        k = float(raw.get("k", 1.5 if method == "iqr" else 3.5))
        if k <= 0:
            raise ValueError("异常值阈值 k 必须为正")
        return cls(method=method, k=k)


@dataclass(frozen=True)
class RuleSpec:
    method: str = "median"
    use_confidence_weight: bool = True
    min_samples: int = 3
    outlier: OutlierSpec | None = None
    params_fingerprint: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuleSpec":
        method = raw.get("method", "median")
        if method not in ("mean", "median"):
            raise ValueError("共识方法仅支持 mean 或 median")
        min_samples = int(raw.get("min_samples", 3))
        if min_samples < 1:
            raise ValueError("最小有效样本数必须 >= 1")
        return cls(
            method=method,
            use_confidence_weight=bool(raw.get("use_confidence_weight", True)),
            min_samples=min_samples,
            outlier=OutlierSpec.from_dict(raw.get("outlier")),
            params_fingerprint=str(raw.get("params_fingerprint", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "use_confidence_weight": self.use_confidence_weight,
            "min_samples": self.min_samples,
            "outlier": None
            if self.outlier is None
            else {"method": self.outlier.method, "k": self.outlier.k},
        }


@dataclass(frozen=True)
class Sample:
    contributor_anon: str
    value: float
    weight: float
    confidence: float
    revision_seq: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Sample":
        confidence = row.get("confidence")
        return cls(
            contributor_anon=str(row["contributor_anon"]),
            value=float(row["value"]),
            weight=max(float(row.get("weight", 1.0)), 0.0),
            confidence=1.0 if confidence is None else min(max(float(confidence), 0.0), 1.0),
            revision_seq=int(row["revision_seq"]),
        )


@dataclass(frozen=True)
class ConsensusResult:
    value: float | None
    method: str
    usable_samples: int
    included_samples: int
    excluded: tuple[str, ...]
    sufficient: bool
    detail: dict[str, Any] = field(default_factory=dict)


def effective_weight(sample: Sample, use_confidence: bool) -> float:
    if sample.weight <= 0:
        return 0.0
    if not use_confidence:
        return sample.weight
    return sample.weight * sample.confidence


def flag_outliers(samples: list[Sample], spec: OutlierSpec) -> dict[str, str]:
    """返回 contributor_anon -> 排除原因；不足 4 个样本时不做剔除（小样本不稳）。"""
    if len(samples) < 4:
        return {}
    values = sorted(s.value for s in samples)
    flagged: dict[str, str] = {}
    if spec.method == "iqr":
        q1 = statistics.quantiles(values, n=4, method="inclusive")[0]
        q3 = statistics.quantiles(values, n=4, method="inclusive")[2]
        iqr = q3 - q1
        low, high = q1 - spec.k * iqr, q3 + spec.k * iqr
        for s in samples:
            if s.value < low:
                flagged[s.contributor_anon] = f"低于IQR下界{low:.6g}"
            elif s.value > high:
                flagged[s.contributor_anon] = f"高于IQR上界{high:.6g}"
    else:  # mad
        center = statistics.median(values)
        deviations = [abs(v - center) for v in values]
        mad = statistics.median(deviations)
        if mad == 0:
            # MAD=0 时退化为：与中位数完全相等以外的点用 k 倍最小正偏差判定，
            # 全部相同则不剔除。
            positive = sorted(d for d in deviations if d > 0)
            if not positive:
                return {}
            scale = positive[0]
        else:
            scale = mad
        for s in samples:
            z = abs(s.value - center) / scale
            if z > spec.k:
                flagged[s.contributor_anon] = f"MAD偏离{z:.2f}>k{spec.k}"
    return flagged


def weighted_median(samples: list[Sample]) -> float:
    """按累计权重达到一半取值；恰好在两样本边界时取两者均值。"""
    ordered = sorted(samples, key=lambda s: s.value)
    total = sum(s.weight for s in ordered)
    if total <= 0:
        return statistics.median([s.value for s in ordered])
    cutoff = total / 2
    cum = 0.0
    for i, s in enumerate(ordered):
        prev_cum = cum
        cum += s.weight
        if cum > cutoff:
            if prev_cum == cutoff and i > 0:
                return (ordered[i - 1].value + s.value) / 2
            return s.value
        if cum == cutoff and i == len(ordered) - 1:
            return s.value
    return ordered[-1].value


def compute_consensus(raw_samples: Iterable[dict[str, Any] | Sample], rule: RuleSpec) -> ConsensusResult:
    """按规则计算共识。返回值为 None 表示样本不足，发布侧应拒绝发布。

    usable_samples：水位内全部样本数（含被剔除/零置信度，保留参与事实）；
    included_samples：实际进入数值计算的样本数。
    """
    samples = [s if isinstance(s, Sample) else Sample.from_row(s) for s in raw_samples]
    excluded: dict[str, str] = {}
    if rule.outlier is not None:
        excluded = flag_outliers(samples, rule.outlier)
    kept = [s for s in samples if s.contributor_anon not in excluded]
    # 置信度为 0 的样本不参与数值计算，但仍计入水位样本事实
    zero_conf = {s.contributor_anon: "置信度为0" for s in kept if s.confidence <= 0}
    calc = [s for s in kept if s.confidence > 0]
    excluded.update(zero_conf)

    if len(kept) < rule.min_samples or not calc:
        return ConsensusResult(
            value=None,
            method=rule.method,
            usable_samples=len(samples),
            included_samples=len(calc),
            excluded=tuple(sorted(excluded)),
            sufficient=False,
            detail={"reason": "有效样本不足", "min_samples": rule.min_samples},
        )

    if rule.method == "median":
        # 中位数以机构为等权单位；启用置信度时采用加权中位数
        if rule.use_confidence_weight:
            weighted = [
                Sample(s.contributor_anon, s.value, effective_weight(s, True), s.confidence, s.revision_seq)
                for s in calc
            ]
            value = weighted_median(weighted)
        else:
            value = statistics.median([s.value for s in calc])
        spread = {
            "min": min(s.value for s in calc),
            "max": max(s.value for s in calc),
        }
    else:
        weights = [effective_weight(s, rule.use_confidence_weight) for s in calc]
        total = sum(weights)
        if total <= 0:
            raise ValueError("有效权重之和为 0")
        value = sum(s.value * w for s, w in zip(calc, weights)) / total
        variance = (
            sum(w * (s.value - value) ** 2 for s, w in zip(calc, weights)) / total
        )
        spread = {"stdev": variance**0.5, "min": min(s.value for s in calc), "max": max(s.value for s in calc)}

    return ConsensusResult(
        value=value,
        method=rule.method,
        usable_samples=len(samples),
        included_samples=len(calc),
        excluded=tuple(sorted(excluded)),
        sufficient=True,
        detail={
            "spread": spread,
            "exclusion_reasons": excluded,
            "calc_samples": len(calc),
        },
    )
