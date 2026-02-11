"""Tests for BookImbalanceTracker — imbalance calculation and EMA smoothing."""

import pytest
from bot_mm.core.book_imbalance import BookImbalanceTracker


# ── Basic imbalance ─────────────────────────────────────────


def test_balanced_book():
    """Equal bid/ask volume → imbalance = 0."""
    tracker = BookImbalanceTracker()
    bids = [(100.0, 10.0), (99.0, 10.0)]
    asks = [(101.0, 10.0), (102.0, 10.0)]
    imb = tracker.update(bids, asks, depth=2)
    assert imb == pytest.approx(0.0)


def test_strong_buy_pressure():
    """All volume on bid side → imbalance = +1."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)  # No smoothing
    bids = [(100.0, 50.0), (99.0, 50.0)]
    asks = []
    imb = tracker.update(bids, asks, depth=5)
    assert imb == pytest.approx(1.0)


def test_strong_sell_pressure():
    """All volume on ask side → imbalance = -1."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)
    bids = []
    asks = [(101.0, 30.0), (102.0, 20.0)]
    imb = tracker.update(bids, asks, depth=5)
    assert imb == pytest.approx(-1.0)


def test_partial_imbalance():
    """More bid volume → positive imbalance between 0 and 1."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)
    bids = [(100.0, 30.0)]
    asks = [(101.0, 10.0)]
    imb = tracker.update(bids, asks, depth=5)
    # (30 - 10) / (30 + 10) = 0.5
    assert imb == pytest.approx(0.5)


def test_depth_limits_levels():
    """Only top N levels are considered."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)
    bids = [(100.0, 10.0), (99.0, 10.0), (98.0, 100.0)]
    asks = [(101.0, 10.0), (102.0, 10.0), (103.0, 100.0)]
    # depth=2: bid_vol=20, ask_vol=20 → 0
    imb = tracker.update(bids, asks, depth=2)
    assert imb == pytest.approx(0.0)


# ── EMA smoothing ──────────────────────────────────────────


def test_ema_smoothing():
    """EMA smooths out sudden changes."""
    tracker = BookImbalanceTracker(ema_alpha=0.3)

    # First update initializes directly
    bids_balanced = [(100.0, 10.0)]
    asks_balanced = [(101.0, 10.0)]
    imb1 = tracker.update(bids_balanced, asks_balanced, depth=5)
    assert imb1 == pytest.approx(0.0)

    # Sudden buy pressure: raw = 1.0, smoothed = 0.3 * 1.0 + 0.7 * 0.0 = 0.3
    bids_heavy = [(100.0, 100.0)]
    asks_light = [(101.0, 0.0)]  # Zero ask → raw = +1.0
    imb2 = tracker.update(bids_heavy, asks_light, depth=5)
    assert imb2 == pytest.approx(0.3, abs=0.001)


def test_ema_converges():
    """Repeated identical updates converge to the raw value."""
    tracker = BookImbalanceTracker(ema_alpha=0.3)
    bids = [(100.0, 30.0)]
    asks = [(101.0, 10.0)]
    # raw = (30-10)/(30+10) = 0.5
    for _ in range(50):
        imb = tracker.update(bids, asks, depth=5)
    assert imb == pytest.approx(0.5, abs=0.01)


# ── Edge cases ─────────────────────────────────────────────


def test_empty_book():
    """Empty book (no bids, no asks) → imbalance = 0."""
    tracker = BookImbalanceTracker()
    imb = tracker.update([], [], depth=5)
    assert imb == pytest.approx(0.0)


def test_reset():
    """Reset clears state."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)
    bids = [(100.0, 50.0)]
    asks = [(101.0, 10.0)]
    tracker.update(bids, asks, depth=5)
    assert tracker.imbalance != 0.0

    tracker.reset()
    assert tracker.imbalance == pytest.approx(0.0)
    assert not tracker._initialized


def test_imbalance_property():
    """Property returns last smoothed value without recomputation."""
    tracker = BookImbalanceTracker(ema_alpha=1.0)
    bids = [(100.0, 30.0)]
    asks = [(101.0, 10.0)]
    result = tracker.update(bids, asks, depth=5)
    assert tracker.imbalance == result
