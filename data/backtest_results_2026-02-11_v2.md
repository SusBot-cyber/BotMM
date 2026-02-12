# Backtest Results — 2026-02-11 (v2, corrected fees)

## Test Parameters
- **Period:** 365 days
- **Capital:** $50,000 ($12,500/asset)
- **Assets:** BTC, ETH, SOL, XRP
- **Maker fee:** +0.015% (HL base tier, COST not rebate)
- **Taker fee:** +0.045%
- **Supervisor:** V3_CONSERVATIVE (window=45d, min_cap=$5K, cut 3-10%, 1% mean-revert)
- **Compound:** BTC/ETH ON, SOL/XRP OFF
- **Features:** bias, toxicity, auto-tune, 2 levels

---

## Portfolio Results

|                       |       EQUAL | SUPERVISOR V3 |  Delta |
|-----------------------|-------------|---------------|--------|
| Gross PnL             |     $70,316 |       $85,380 |        |
| Fees (maker 0.015%)   |    -$18,703 |      -$22,710 |        |
| **Net PnL**           | **$51,613** |   **$62,670** | +21.4% |
| Return                |      103.2% |        125.3% |        |
| Final Equity          |    $101,613 |      $112,670 |        |
| Sharpe                |        16.4 |          16.1 |        |
| Max Drawdown          |        $639 |          $735 |        |
| Profitable Days       | 319/365 87% |   320/365 88% |        |
| Monthly Net           |      $4,301 |        $5,222 |        |
| Daily Net             |        $141 |          $172 |        |
| Fee % of Gross        |       26.6% |         26.6% |        |

---

## Per-Asset (Supervisor V3)

| Asset     |   Net PnL | Gross(est) | Fees(est) | Return | Final Effective | Mode     |
|-----------|-----------|------------|-----------|--------|-----------------|----------|
| BTC       |   $17,599 |    $23,976 |    $6,377 | 140.8% |         $30,099 | COMPOUND |
| ETH       |   $20,165 |    $27,472 |    $7,307 | 161.3% |         $32,665 | COMPOUND |
| SOL       |   $14,130 |    $19,250 |    $5,120 | 113.0% |         $12,500 | FIXED    |
| XRP       |   $11,877 |    $16,181 |    $4,304 |  95.0% |         $12,500 | FIXED    |
| **TOTAL** |   **$63,771** | **$86,880** | **$23,109** | **127.5%** | **$87,764** |    |

---

## Per-Asset Raw ($12.5K fixed, no supervisor)

### PnL & Fees

| Asset     | Gross PnL |   Fees  |  Net PnL | Return | Sharpe | Compound |
|-----------|-----------|---------|----------|--------|--------|----------|
| BTC       |   $12,109 |  $3,030 |   $9,080 |  72.6% |   11.0 | ON       |
| ETH       |   $16,045 |  $4,077 |  $11,969 |  95.8% |    9.0 | ON       |
| SOL       |   $15,372 |  $4,547 |  $10,823 |  86.6% |   10.7 | OFF      |
| XRP       |   $16,225 |  $4,238 |  $11,986 |  95.9% |    9.1 | OFF      |
| **TOTAL** |   **$59,751** | **$15,891** | **$43,858** | **87.7%** | —  |          |

### Trading Activity

| Asset     |  Fills |  Fills/d | RndTrips |    Volume |  Vol/day | Partials |
|-----------|--------|----------|----------|-----------|----------|----------|
| BTC       | 15,189 |       42 |    8,838 |    $28.5M |     $78K |    5,962 |
| ETH       | 18,624 |       51 |   11,023 |    $34.9M |     $96K |    3,820 |
| SOL       | 19,431 |       53 |   12,331 |    $36.4M |    $100K |    3,189 |
| XRP       | 18,898 |       52 |   11,732 |    $35.4M |     $97K |    3,757 |
| **TOTAL** | **72,142** | **198** | **43,924** | **$135.3M** | **$371K** | **16,728** |

### Risk Metrics

| Asset | MaxDD | DD/PnL% | Prof.days% | AvgInv$ | MaxInv$ | RiskHalts |
|-------|-------|---------|------------|---------|---------|-----------|
| BTC   |  $184 |    2.0% |        80% |  $1,309 |  $5,698 |         0 |
| ETH   |  $361 |    3.0% |        77% |  $1,303 |  $5,915 |         0 |
| SOL   |  $558 |    5.2% |        79% |    $950 |  $4,332 |         0 |
| XRP   |  $286 |    2.4% |        76% |  $1,085 |  $5,528 |         0 |

### Spread & ML

| Asset | Sprd.Quoted | Sprd.Captured | Toxicity | ToxFill% | AutoTune adj. |
|-------|-------------|---------------|----------|----------|---------------|
| BTC   |      20.0bp |        30.1bp |    0.379 |    32.0% |           250 |
| ETH   |      20.0bp |        38.3bp |    0.361 |    32.0% |           278 |
| SOL   |      20.0bp |        33.5bp |    0.395 |    38.0% |           139 |
| XRP   |      20.0bp |        36.0bp |    0.389 |    34.0% |           197 |

### Daily Averages

| Asset     | PnL/day  | Fee/day  | Gross/day | PnL/fill | Fee/fill |
|-----------|----------|----------|-----------|----------|----------|
| BTC       |   $24.88 |    $8.30 |    $33.18 |   $0.598 |   $0.200 |
| ETH       |   $32.79 |   $11.17 |    $43.96 |   $0.643 |   $0.219 |
| SOL       |   $29.65 |   $12.46 |    $42.11 |   $0.557 |   $0.234 |
| XRP       |   $32.84 |   $11.61 |    $44.45 |   $0.634 |   $0.224 |
| **TOTAL** | **$120.16** | **$43.54** | **$163.70** | **$0.608** | **$0.220** |

---

## Summary Stats

| Metric             |                       Value |
|--------------------|-----------------------------|
| Gross PnL          |                     $85,380 |
| Total Fees         |      $22,710 (26.6% gross)  |
| Net PnL            |                     $62,670 |
| Total Volume       |                     $193.3M |
| Total Fills        |                       ~103K |
| Fee/Volume         |                      0.012% |
| Net/Volume         |                      0.032% |
| Net profit/fill    |                      $0.608 |
| Fee cost/fill      |                      $0.220 |
| Net/Fee ratio      |                       2.76× |

---

## Supervisor Tuning Results

Tested 6 variants on 365d data:

| Variant              | Config                              | Net PnL |  Return | Sharpe | MaxDD |
|----------------------|-------------------------------------|---------|---------|--------|-------|
| V0_CURRENT (old)     | w=14d, min=$500, cut=30/10%         | $60,944 |  121.9% |   14.4 |  $899 |
| V1_GENTLE            | w=30d, min=$2.5K, cut=15/5%         | $64,934 |  129.9% |   15.3 |  $900 |
| V2_SLOW_REVERT       | V1 + 2% mean_revert                 | $64,872 |  129.7% |   15.1 |  $899 |
| **V3_CONSERVATIVE**  | **w=45d, min=$5K, cut=10/3%, 1% mr** | **$65,440** | **130.9%** | **14.9** | **$873** |
| V4_RISK_ONLY         | fixed alloc, risk adj only          | $63,816 |  127.6% |   15.3 |  $722 |
| V5_EQUAL_WEIGHT      | no supervisor                       | $65,063 |  130.1% |   14.8 |  $865 |

**V3_CONSERVATIVE selected** — beats old V0 by +$4,500 (+9%), gentle enough to not destroy allocations.

---

## HYPE Staking Analysis

At $50K capital, only 10 HYPE stake ($300, 5% fee rabat) is profitable:

| Rabat | HYPE |    Cost | Savings/yr | Same $ in bot | Net benefit      |
|-------|------|---------|------------|---------------|------------------|
| 5%    |   10 |    $300 |     $1,136 |          $376 | **+$760 ✅**     |
| 10%   |  100 |  $3,000 |     $2,271 |        $3,759 | -$1,488 ❌       |
| 15%   |  1K  | $30,000 |     $3,407 |       $37,590 | -$34,183 ❌      |

100 HYPE stake becomes profitable at bot capital >$83K.

---

## Previous Results (DEPRECATED — used wrong fee -0.015% rebate)

See `data/backtest_results_2026-02-11.md` for old results with incorrect rebate assumption.
Those results overestimate net PnL by ~$16K on 225d.

---

*Generated: 2026-02-11, commit: pending*
*Supervisor: V3_CONSERVATIVE, Fee: +0.015% maker (HL base tier)*
