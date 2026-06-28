"""Regression tests for the cold-start and low-variance guards on the
z-score anomaly detector (src/victoriametrics/client.py:_z_score).

False positives historically appeared in the first few cycles after the
service started, because a tiny window produces a near-zero standard
deviation and any small deviation yields a huge z. The guards below ensure
the detector stays silent until the window is both large enough and not
essentially constant.
"""

from __future__ import annotations

from src.config import settings
from src.victoriametrics.client import VictoriaMetricsClient


class _FakeSession:
    """Stand-in for aiohttp.ClientSession; the tests never send real HTTP."""

    async def close(self) -> None:
        return None


def _client() -> VictoriaMetricsClient:
    return VictoriaMetricsClient("http://vm:8428", session=_FakeSession())  # type: ignore[arg-type]


def test_cold_start_returns_zero_when_window_too_small():
    """5 identical samples + a divergent current value -> 0.0 (window < min)."""
    client = _client()
    history = [42.0] * 5
    assert client._z_score(history, current=999.0) == 0.0


def test_low_std_returns_zero_when_window_is_constant():
    """15 zeros + a spike -> 0.0 (std below ANOMALY_MIN_STD_THRESHOLD)."""
    client = _client()
    history = [0.0] * 15
    assert client._z_score(history, current=100.0) == 0.0


def test_realistic_variance_produces_high_score_on_spike():
    """15 samples with realistic variance + a clear spike -> z-score > 3."""
    client = _client()
    history = [
        10.0, 12.0, 9.5, 11.0, 10.5,
        13.0, 8.0, 11.5, 10.0, 12.5,
        9.0, 10.8, 11.2, 9.7, 10.3,
    ]
    score = client._z_score(history, current=1000.0)
    assert score > 3.0


def test_low_baseline_mean_guard_blocks_then_falls_back(monkeypatch):
    """Low-volume regime (defect 4): 15 samples with mean ~= 1.07 and std ~= 0.88,
    plus a current value of 10. Under the default guard (min_baseline_mean=10.0)
    the score must be zero; lowering the threshold to 0.5 must let the normal
    z-score through and produce a value > 3 — confirming no regression in the
    z-score path itself."""
    client = _client()
    history = [1, 0, 2, 1, 0, 1, 3, 1, 0, 2, 1, 0, 1, 2, 1]  # mean ~1.07, std ~0.88

    # Default: 10.0 > 1.07 -> low-baseline guard fires.
    monkeypatch.setattr(settings, "anomaly_min_baseline_mean", 10.0)
    assert client._z_score(history, current=10.0) == 0.0

    # Lowered threshold lets the actual z-score through.
    monkeypatch.setattr(settings, "anomaly_min_baseline_mean", 0.5)
    score = client._z_score(history, current=10.0)
    assert score > 3.0, f"expected z>3 once the baseline guard is cleared; got {score}"
