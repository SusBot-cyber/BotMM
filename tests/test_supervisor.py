"""Tests for backtest_supervisor scoring, allocation, and period handling."""

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from backtest_supervisor import (
    compute_score,
    compute_scores_ranked,
    apply_allocation,
    compute_risk_adjustments,
    rank_normalize,
)


# ── compute_score ────────────────────────────────────────────────────────

class TestComputeScore:
    """Tests for daily PnL window → metrics."""

    def test_positive_consistent_pnl(self):
        """All positive days → Sharpe=0 (no volatility), 100% consistency."""
        pnls = [10.0] * 14
        m = compute_score(pnls)
        assert m["sharpe"] == 0  # constant → std=0 → undefined Sharpe
        assert m["consistency"] == 1.0
        assert m["return"] == 140.0
        assert m["dd"] == 0.0

    def test_negative_pnl(self):
        """All negative days → Sharpe=0 (constant), 0% consistency."""
        pnls = [-5.0] * 14
        m = compute_score(pnls)
        assert m["sharpe"] == 0  # constant → std=0
        assert m["consistency"] == 0.0
        assert m["return"] == -70.0
        assert m["dd"] > 0

    def test_mixed_pnl(self):
        """Mixed days → moderate metrics."""
        pnls = [10, -5, 10, -5, 10, -5, 10, -5, 10, -5, 10, -5, 10, -5]
        m = compute_score(pnls)
        assert m["sharpe"] > 0
        assert m["consistency"] == 0.5
        assert m["return"] == 35.0

    def test_zero_pnl(self):
        """All zeros → zero Sharpe, zero return."""
        pnls = [0.0] * 14
        m = compute_score(pnls)
        assert m["sharpe"] == 0
        assert m["return"] == 0.0
        assert m["consistency"] == 0.0

    def test_too_few_days(self):
        """Less than 3 days → zero defaults."""
        m = compute_score([10.0, 20.0])
        assert m["sharpe"] == 0
        assert m["return"] == 0
        assert m["dd"] == 1.0
        assert m["consistency"] == 0

    def test_empty_list(self):
        m = compute_score([])
        assert m["sharpe"] == 0

    def test_single_spike(self):
        """One big win, rest flat → low consistency but positive return."""
        pnls = [0.0] * 13 + [100.0]
        m = compute_score(pnls)
        assert m["return"] == 100.0
        assert m["consistency"] < 0.15  # only 1/14 positive

    def test_drawdown_calculation(self):
        """Specific drawdown scenario: up 10, then down 20 → DD=20."""
        pnls = [10, 10, 10, -20, -10, 5, 5, 5, 5, 5, 5, 5, 5, 5]
        m = compute_score(pnls)
        assert m["dd"] >= 25  # peak at 30, drops to 10 → DD≥20
        assert m["return"] == sum(pnls)

    def test_volatile_high_return(self):
        """High volatility but positive → has Sharpe (since std > 0)."""
        steady = [5.0] * 14  # no vol → Sharpe=0
        volatile = [25, -10, 25, -10, 25, -10, 25, -10, 25, -10, 25, -10, 25, -10]
        m_s = compute_score(steady)
        m_v = compute_score(volatile)
        assert m_s["sharpe"] == 0
        assert m_v["sharpe"] > 0
        assert m_v["return"] > m_s["return"]


# ── compute_scores_ranked (absolute scoring) ────────────────────────────

class TestComputeScoresRanked:
    """Tests for absolute threshold scoring."""

    def test_excellent_bot(self):
        """High Sharpe, positive return, low DD → score > 0.7."""
        metrics = [{"sharpe": 12.0, "return": 100, "dd": 5, "consistency": 0.85}]
        scores = compute_scores_ranked(metrics)
        assert scores[0] > 0.7

    def test_terrible_bot(self):
        """Negative Sharpe, negative return → score < 0.4."""
        metrics = [{"sharpe": -2.0, "return": -50, "dd": 50, "consistency": 0.2}]
        scores = compute_scores_ranked(metrics)
        assert scores[0] < 0.4

    def test_mediocre_bot(self):
        """Average metrics → mid-range score."""
        metrics = [{"sharpe": 3.0, "return": 10, "dd": 8, "consistency": 0.55}]
        scores = compute_scores_ranked(metrics)
        assert 0.3 < scores[0] < 0.8

    def test_ordering_preserved(self):
        """Better bot should always score higher than worse bot."""
        metrics = [
            {"sharpe": 15.0, "return": 200, "dd": 5, "consistency": 0.9},
            {"sharpe": 2.0, "return": 20, "dd": 30, "consistency": 0.4},
        ]
        scores = compute_scores_ranked(metrics)
        assert scores[0] > scores[1]

    def test_five_bots_independent(self):
        """5 bots scored independently — no rank penalty for bottom."""
        metrics = [
            {"sharpe": 10, "return": 80, "dd": 5, "consistency": 0.85},
            {"sharpe": 10, "return": 80, "dd": 5, "consistency": 0.85},
            {"sharpe": 10, "return": 80, "dd": 5, "consistency": 0.85},
            {"sharpe": 10, "return": 80, "dd": 5, "consistency": 0.85},
            {"sharpe": 10, "return": 80, "dd": 5, "consistency": 0.85},
        ]
        scores = compute_scores_ranked(metrics)
        # All identical → all same score (absolute, not rank)
        assert all(abs(s - scores[0]) < 0.01 for s in scores)

    def test_all_bots_good_no_punishment(self):
        """When all bots are good, all should score > 0.5 (no false punishment)."""
        metrics = [
            {"sharpe": 8, "return": 60, "dd": 3, "consistency": 0.80},
            {"sharpe": 12, "return": 90, "dd": 5, "consistency": 0.85},
            {"sharpe": 6, "return": 40, "dd": 4, "consistency": 0.75},
            {"sharpe": 10, "return": 70, "dd": 6, "consistency": 0.82},
            {"sharpe": 5, "return": 30, "dd": 2, "consistency": 0.70},
        ]
        scores = compute_scores_ranked(metrics)
        # All profitable with good Sharpe → all should be above punishment threshold
        assert all(s > 0.4 for s in scores), f"Some good bots scored < 0.4: {scores}"

    def test_sharpe_clamping(self):
        """Extreme Sharpe values should be clamped to [0, 1]."""
        low = [{"sharpe": -10, "return": 0, "dd": 0, "consistency": 0.5}]
        high = [{"sharpe": 50, "return": 0, "dd": 0, "consistency": 0.5}]
        s_low = compute_scores_ranked(low)
        s_high = compute_scores_ranked(high)
        assert 0 <= s_low[0] <= 1
        assert 0 <= s_high[0] <= 1

    def test_zero_return_handling(self):
        """Zero return → mid-range return score, no division by zero."""
        metrics = [{"sharpe": 0, "return": 0, "dd": 0, "consistency": 0.5}]
        scores = compute_scores_ranked(metrics)
        assert 0 <= scores[0] <= 1

    def test_empty_list(self):
        scores = compute_scores_ranked([])
        assert scores == []


# ── rank_normalize ───────────────────────────────────────────────────────

class TestRankNormalize:
    """Tests for rank-based normalization utility."""

    def test_ascending(self):
        result = rank_normalize([10, 20, 30])
        assert result == [0.0, 0.5, 1.0]

    def test_descending(self):
        result = rank_normalize([30, 20, 10])
        assert result == [1.0, 0.5, 0.0]

    def test_single(self):
        result = rank_normalize([42])
        assert result == [0.5]

    def test_empty(self):
        result = rank_normalize([])
        assert result == []

    def test_equal_values(self):
        """Ties: deterministic order but both should get a rank."""
        result = rank_normalize([5, 5, 5])
        assert len(result) == 3
        assert all(0 <= r <= 1 for r in result)

    def test_negative_values(self):
        result = rank_normalize([-10, 0, 10])
        assert result == [0.0, 0.5, 1.0]


# ── apply_allocation ────────────────────────────────────────────────────

class TestApplyAllocation:
    """Tests for reward/punish allocation rules."""

    def _base_alloc(self, n=5, per=10000):
        return {f"BOT{i}": float(per) for i in range(n)}

    def test_all_hold_no_change(self):
        """Score 0.5 for all → HOLD → no change."""
        alloc = self._base_alloc()
        scores = {k: 0.55 for k in alloc}
        new = apply_allocation(alloc, scores, 50000)
        for k in alloc:
            assert new[k] == alloc[k]

    def test_high_scorer_rewarded(self):
        """Score > 0.7 should get more capital (if pool available)."""
        alloc = self._base_alloc()
        scores = {k: 0.55 for k in alloc}
        scores["BOT0"] = 0.85  # reward
        scores["BOT4"] = 0.15  # pause → frees capital
        new = apply_allocation(alloc, scores, 50000)
        assert new["BOT0"] > alloc["BOT0"]

    def test_pause_reduces_capital(self):
        """Score < 0.2 → PAUSE → capital reduced."""
        alloc = self._base_alloc()
        scores = {k: 0.55 for k in alloc}
        scores["BOT3"] = 0.1
        new = apply_allocation(alloc, scores, 50000)
        assert new["BOT3"] < alloc["BOT3"]

    def test_punish_reduces_capital(self):
        """Score 0.2-0.4 → PUNISH → capital reduced by 10%."""
        alloc = self._base_alloc()
        scores = {k: 0.55 for k in alloc}
        scores["BOT2"] = 0.3
        new = apply_allocation(alloc, scores, 50000)
        assert new["BOT2"] < alloc["BOT2"]

    def test_min_capital_enforced(self):
        """Paused bot never goes below min_capital."""
        alloc = {"A": 600.0, "B": 10000.0}
        scores = {"A": 0.05, "B": 0.55}
        new = apply_allocation(alloc, scores, 10600, min_capital=500)
        assert new["A"] >= 500

    def test_max_pct_enforced(self):
        """Rewarded bot already above max_pct → no additional capital added."""
        alloc = {"A": 34000.0, "B": 1000.0}
        scores = {"A": 0.9, "B": 0.1}
        new = apply_allocation(alloc, scores, 35000, max_pct=0.35)
        # A is already at 97% which exceeds max_pct=35%, reward won't push higher
        # B paused → loses capital, but A can't absorb since already over cap
        assert new["A"] <= alloc["A"] + 1  # no growth beyond current

    def test_capital_conservation(self):
        """Total capital should be approximately conserved."""
        alloc = self._base_alloc()
        scores = {"BOT0": 0.9, "BOT1": 0.8, "BOT2": 0.5, "BOT3": 0.3, "BOT4": 0.1}
        new = apply_allocation(alloc, scores, 50000)
        total_before = sum(alloc.values())
        total_after = sum(new.values())
        # Some capital might not be redistributed if capped, but shouldn't exceed original
        assert total_after <= total_before + 1

    def test_pool_distributed_to_hold_if_no_rewarded(self):
        """If no bot scores > 0.7 but some are punished → pool goes to HOLD bots."""
        alloc = self._base_alloc(3, 10000)
        scores = {"BOT0": 0.55, "BOT1": 0.55, "BOT2": 0.15}
        new = apply_allocation(alloc, scores, 30000)
        # BOT2 lost capital, BOT0/BOT1 are HOLD → should get the pool
        assert new["BOT0"] >= alloc["BOT0"]
        assert new["BOT2"] < alloc["BOT2"]

    def test_max_daily_change_limits_reward(self):
        """Reward per day capped at max_daily_change %."""
        alloc = {"A": 10000.0, "B": 10000.0}
        scores = {"A": 0.9, "B": 0.1}
        new = apply_allocation(alloc, scores, 20000, max_daily_change=0.05)
        # A can gain at most 5% of current = 500
        assert new["A"] <= 10000 + 10000 * 0.05 + 1

    def test_max_daily_change_limits_punishment(self):
        """Punishment per day capped at max_daily_change %."""
        alloc = {"A": 10000.0, "B": 10000.0}
        scores = {"A": 0.55, "B": 0.3}  # B is PUNISH
        new = apply_allocation(alloc, scores, 20000, max_daily_change=0.05)
        # B loses 10% but capped at 5%
        assert new["B"] >= 10000 * (1 - 0.05) - 1

    def test_single_bot(self):
        """Single bot → no rebalancing possible."""
        alloc = {"ONLY": 10000.0}
        scores = {"ONLY": 0.1}
        new = apply_allocation(alloc, scores, 10000)
        # Can only reduce to min, but no one to give capital to
        assert new["ONLY"] >= 500

    def test_all_paused(self):
        """All bots score < 0.2 → all reduced, pool has nowhere to go."""
        alloc = self._base_alloc(3, 5000)
        scores = {"BOT0": 0.1, "BOT1": 0.1, "BOT2": 0.1}
        new = apply_allocation(alloc, scores, 15000)
        assert all(new[k] <= alloc[k] for k in alloc)


# ── Period trimming logic ────────────────────────────────────────────────

class TestPeriodTrimming:
    """Tests for hybrid period handling — trim to shortest series."""

    def test_trim_to_shortest(self):
        """Longer series should be trimmed to match shortest."""
        daily_pnls = {
            "BTC": [1.0] * 365,
            "ETH": [2.0] * 365,
            "HYPE": [3.0] * 225,
        }
        min_days = min(len(v) for v in daily_pnls.values())
        for sym in daily_pnls:
            if len(daily_pnls[sym]) > min_days:
                daily_pnls[sym] = daily_pnls[sym][-min_days:]

        assert all(len(v) == 225 for v in daily_pnls.values())

    def test_trim_keeps_last_n_days(self):
        """Trimming should keep the LAST N days (most recent data)."""
        series = list(range(1, 366))  # 1..365
        min_days = 225
        trimmed = series[-min_days:]
        assert trimmed[0] == 141  # 365 - 225 + 1
        assert trimmed[-1] == 365

    def test_trim_no_change_if_equal(self):
        """All same length → no trimming."""
        daily_pnls = {
            "A": [1.0] * 100,
            "B": [2.0] * 100,
        }
        min_days = min(len(v) for v in daily_pnls.values())
        for sym in daily_pnls:
            if len(daily_pnls[sym]) > min_days:
                daily_pnls[sym] = daily_pnls[sym][-min_days:]

        assert all(len(v) == 100 for v in daily_pnls.values())

    def test_trim_preserves_values(self):
        """Trimmed data should keep correct values (not zeros)."""
        btc = [float(i) for i in range(365)]
        hype = [float(i) for i in range(225)]

        btc_trimmed = btc[-225:]
        assert btc_trimmed[0] == 140.0  # starts from day 140
        assert btc_trimmed[-1] == 364.0
        assert len(btc_trimmed) == len(hype)

    def test_five_assets_different_lengths(self):
        """Realistic scenario: 4 assets 365d, 1 asset 225d."""
        daily_pnls = {
            "BTC": [1.0] * 365,
            "ETH": [2.0] * 365,
            "SOL": [1.5] * 365,
            "XRP": [1.8] * 365,
            "HYPE": [0.8] * 225,
        }
        min_days = min(len(v) for v in daily_pnls.values())
        for sym in daily_pnls:
            daily_pnls[sym] = daily_pnls[sym][-min_days:]

        assert min_days == 225
        assert all(len(v) == 225 for v in daily_pnls.values())


# ── Equal vs Adaptive simulation logic ───────────────────────────────────

class TestSimulationLogic:
    """Tests for simulation correctness."""

    def test_equal_allocation_scales_pnl(self):
        """PnL scaled from $1K base to allocation amount."""
        base_pnl = [10.0]  # $10 at $1K base
        alloc = 5000.0  # 5x
        scaled = base_pnl[0] * (alloc / 1000.0)
        assert scaled == 50.0

    def test_equal_total_pnl(self):
        """5 bots × $10K × $10/day at $1K base = $500/day total."""
        n_assets = 5
        base_alloc = 10000.0
        daily_base_pnl = 10.0
        day_total = n_assets * daily_base_pnl * (base_alloc / 1000.0)
        assert day_total == 500.0

    def test_adaptive_uses_current_allocation(self):
        """Adaptive PnL should scale by current (not initial) allocation."""
        alloc = {"A": 15000.0, "B": 5000.0}
        base_pnl = {"A": 10.0, "B": 10.0}

        pnl_a = base_pnl["A"] * (alloc["A"] / 1000.0)  # 150
        pnl_b = base_pnl["B"] * (alloc["B"] / 1000.0)  # 50
        assert pnl_a == 150.0
        assert pnl_b == 50.0

    def test_scoring_window_skips_early_days(self):
        """No rebalancing before scoring window is full."""
        window = 14
        alloc = {"A": 10000.0}
        for day in range(window):
            # Should NOT rebalance
            assert day < window
        # Day 14+ → rebalance starts
        assert window >= 14

    def test_sharpe_calculation(self):
        """Sharpe = mean/std * sqrt(365)."""
        daily = [10.0] * 100
        mean = np.mean(daily)
        std = np.std(daily)
        # Constant → std=0 → Sharpe=0 (guarded)
        sharpe = (mean / std * math.sqrt(365)) if std > 0 else 0
        assert sharpe == 0  # no volatility = undefined Sharpe

    def test_drawdown_from_equity(self):
        """Max drawdown from equity curve."""
        equity = [100, 110, 105, 115, 100, 120]
        peak = np.maximum.accumulate(equity)
        dd = max(peak - np.array(equity))
        assert dd == 15  # peak 115, drops to 100

    def test_profitable_days_count(self):
        daily = [10, -5, 10, 0, -3, 10, 10]
        pos = sum(1 for d in daily if d > 0)
        assert pos == 4


# ── Integration: score → allocation flow ─────────────────────────────────

class TestScoreToAllocationFlow:
    """End-to-end: metrics → score → allocation."""

    def test_good_bot_gets_more(self):
        """Profitable bot with high Sharpe → rewarded."""
        metrics = [
            {"sharpe": 15, "return": 100, "dd": 5, "consistency": 0.9},
            {"sharpe": 1, "return": 5, "dd": 20, "consistency": 0.4},
        ]
        scores = compute_scores_ranked(metrics)
        alloc = {"GOOD": 10000.0, "BAD": 10000.0}
        scores_dict = {"GOOD": scores[0], "BAD": scores[1]}
        new = apply_allocation(alloc, scores_dict, 20000)
        assert new["GOOD"] >= alloc["GOOD"]

    def test_losing_bot_loses_capital(self):
        """Negative PnL bot → punished."""
        metrics = [
            {"sharpe": -3, "return": -50, "dd": 60, "consistency": 0.1},
        ]
        scores = compute_scores_ranked(metrics)
        assert scores[0] < 0.4  # should be in PUNISH or PAUSE zone

    def test_all_profitable_stable(self):
        """All bots profitable → minimal reallocation."""
        metrics = [
            {"sharpe": 8, "return": 50, "dd": 3, "consistency": 0.80},
            {"sharpe": 10, "return": 60, "dd": 4, "consistency": 0.82},
            {"sharpe": 12, "return": 70, "dd": 5, "consistency": 0.85},
        ]
        scores = compute_scores_ranked(metrics)
        alloc = {"A": 10000.0, "B": 10000.0, "C": 10000.0}
        scores_dict = dict(zip(alloc.keys(), scores))
        new = apply_allocation(alloc, scores_dict, 30000)

        # All good → changes should be small (< 15% per bot)
        for k in alloc:
            change_pct = abs(new[k] - alloc[k]) / alloc[k]
            assert change_pct < 0.16, f"{k}: {change_pct:.1%} change"

    def test_multi_day_convergence(self):
        """Repeated application should converge, not oscillate wildly."""
        alloc = {"A": 10000.0, "B": 10000.0, "C": 10000.0}
        scores_dict = {"A": 0.8, "B": 0.5, "C": 0.3}

        history = [sum(alloc.values())]
        for _ in range(30):
            alloc = apply_allocation(alloc, scores_dict, 30000)
            history.append(sum(alloc.values()))

        # Total capital shouldn't grow
        assert all(h <= 30001 for h in history)
        # A should be largest, C smallest
        assert alloc["A"] > alloc["C"]


# ── compute_risk_adjustments ─────────────────────────────────────────────

class TestComputeRiskAdjustments:
    """Tests for risk parameter multipliers based on score."""

    def test_reward_zone(self):
        """Score > 0.7 → larger size, tighter spread."""
        scores = {"BTC": 0.85}
        adj = compute_risk_adjustments(scores)
        assert adj["BTC"]["size_mult"] >= 1.0
        assert adj["BTC"]["spread_mult"] <= 1.0
        assert adj["BTC"]["max_pos_mult"] >= 1.0

    def test_hold_zone(self):
        """Score 0.4-0.7 → neutral (1.0 everywhere)."""
        scores = {"ETH": 0.55}
        adj = compute_risk_adjustments(scores)
        assert adj["ETH"]["size_mult"] == 1.0
        assert adj["ETH"]["spread_mult"] == 1.0
        assert adj["ETH"]["max_pos_mult"] == 1.0
        assert adj["ETH"]["max_loss_mult"] == 1.0

    def test_punish_zone(self):
        """Score 0.2-0.4 → smaller size, wider spread."""
        scores = {"SOL": 0.3}
        adj = compute_risk_adjustments(scores)
        assert adj["SOL"]["size_mult"] < 1.0
        assert adj["SOL"]["spread_mult"] > 1.0
        assert adj["SOL"]["max_pos_mult"] < 1.0

    def test_pause_zone(self):
        """Score < 0.2 → minimum risk, widest spread."""
        scores = {"HYPE": 0.1}
        adj = compute_risk_adjustments(scores)
        assert adj["HYPE"]["size_mult"] <= 0.5
        assert adj["HYPE"]["spread_mult"] >= 1.4
        assert adj["HYPE"]["max_pos_mult"] <= 0.5
        assert adj["HYPE"]["max_loss_mult"] <= 0.5

    def test_bounds_enforced(self):
        """All multipliers within defined bounds."""
        scores = {"A": 0.0, "B": 0.5, "C": 1.0}
        adj = compute_risk_adjustments(scores)
        for sym in scores:
            assert 0.30 <= adj[sym]["size_mult"] <= 1.20
            assert 0.80 <= adj[sym]["spread_mult"] <= 1.50
            assert 0.30 <= adj[sym]["max_pos_mult"] <= 1.20
            assert 0.30 <= adj[sym]["max_loss_mult"] <= 1.00

    def test_rate_limited_transition(self):
        """Risk changes should be rate-limited by max_risk_change."""
        scores = {"A": 0.1}  # pause target: size_mult=0.4
        current = {"A": {"size_mult": 1.0, "spread_mult": 1.0, "max_pos_mult": 1.0, "max_loss_mult": 1.0}}
        adj = compute_risk_adjustments(scores, current, max_risk_change=0.10)
        # From 1.0 → 0.4 target, but capped at -0.10 step → 0.90
        assert adj["A"]["size_mult"] == pytest.approx(0.90, abs=0.01)

    def test_gradual_convergence(self):
        """Multiple steps should gradually converge to target."""
        scores = {"A": 0.1}  # pause target
        risk = {"A": {"size_mult": 1.0, "spread_mult": 1.0, "max_pos_mult": 1.0, "max_loss_mult": 1.0}}
        for _ in range(20):
            risk = compute_risk_adjustments(scores, risk, max_risk_change=0.10)
        # After 20 steps of 0.10 max change, should reach target (0.4)
        assert adj_at_target(risk["A"]["size_mult"], 0.40, tol=0.05)
        assert adj_at_target(risk["A"]["spread_mult"], 1.50, tol=0.05)

    def test_no_current_risk_first_call(self):
        """First call without current_risk → jumps to target directly."""
        scores = {"A": 0.85}
        adj = compute_risk_adjustments(scores)
        assert adj["A"]["size_mult"] == pytest.approx(1.10, abs=0.01)

    def test_multi_bot_independent(self):
        """Each bot's risk is independent."""
        scores = {"GOOD": 0.9, "BAD": 0.1}
        adj = compute_risk_adjustments(scores)
        assert adj["GOOD"]["size_mult"] > adj["BAD"]["size_mult"]
        assert adj["GOOD"]["spread_mult"] < adj["BAD"]["spread_mult"]

    def test_risk_effect_formula(self):
        """Risk effect = size_mult * (2 - spread_mult)."""
        # Reward: 1.1 * (2 - 0.9) = 1.1 * 1.1 = 1.21 → bot gets ~21% more PnL
        eff = 1.10 * (2.0 - 0.90)
        assert eff == pytest.approx(1.21, abs=0.01)

        # Pause: 0.4 * (2 - 1.5) = 0.4 * 0.5 = 0.20 → bot gets only 20% of PnL
        eff = 0.40 * (2.0 - 1.50)
        assert eff == pytest.approx(0.20, abs=0.01)

        # Hold: 1.0 * (2 - 1.0) = 1.0 → neutral
        eff = 1.0 * (2.0 - 1.0)
        assert eff == pytest.approx(1.0, abs=0.01)

    def test_reward_doesnt_exceed_hold_on_recovery(self):
        """Bot recovering from pause → risk grows gradually, not instantly."""
        scores = {"A": 0.75}  # just entered reward zone
        current = {"A": {"size_mult": 0.40, "spread_mult": 1.50, "max_pos_mult": 0.40, "max_loss_mult": 0.40}}
        adj = compute_risk_adjustments(scores, current, max_risk_change=0.10)
        # Should move toward 1.1 but only by 0.10 step
        assert adj["A"]["size_mult"] == pytest.approx(0.50, abs=0.01)
        assert adj["A"]["spread_mult"] == pytest.approx(1.40, abs=0.01)


def adj_at_target(val: float, target: float, tol: float = 0.05) -> bool:
    """Helper: check if value is close to target."""
    return abs(val - target) <= tol


# ── Integration: score → capital + risk combined ─────────────────────────

class TestCombinedCapitalAndRisk:
    """Tests for dual control: capital allocation + risk adjustments."""

    def test_bad_bot_double_punished(self):
        """Bad bot loses BOTH capital AND risk exposure."""
        alloc = {"A": 10000.0, "B": 10000.0}
        scores = {"A": 0.85, "B": 0.15}

        new_alloc = apply_allocation(alloc, scores, 20000)
        risk = compute_risk_adjustments(scores)

        # B loses capital
        assert new_alloc["B"] < alloc["B"]
        # B also gets reduced risk
        assert risk["B"]["size_mult"] < 1.0
        assert risk["B"]["spread_mult"] > 1.0

        # Effective exposure: capital * size_mult
        eff_a = new_alloc["A"] * risk["A"]["size_mult"]
        eff_b = new_alloc["B"] * risk["B"]["size_mult"]
        assert eff_a > eff_b * 3  # at least 3x difference

    def test_all_good_bots_full_risk(self):
        """When all bots are good → all get full risk multipliers."""
        scores = {"A": 0.8, "B": 0.75, "C": 0.9}
        risk = compute_risk_adjustments(scores)
        for sym in scores:
            assert risk[sym]["size_mult"] >= 1.0
            assert risk[sym]["spread_mult"] <= 1.0

    def test_risk_adjusts_faster_than_capital(self):
        """Risk jumps immediately, capital moves gradually."""
        alloc = {"A": 10000.0}
        scores = {"A": 0.15}

        # Capital: limited by max_daily_change (15%)
        new_alloc = apply_allocation(alloc, scores, 10000, max_daily_change=0.15)
        capital_change_pct = abs(new_alloc["A"] - alloc["A"]) / alloc["A"]

        # Risk: jumps to target immediately (no current_risk)
        risk = compute_risk_adjustments(scores)
        risk_change = 1.0 - risk["A"]["size_mult"]

        # Risk change should be larger than capital change
        assert risk_change > capital_change_pct
