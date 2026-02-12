# Strategy Expansion Plan — Multi-Strategy Architecture

## Overview

Rozszerzenie ekosystemu o 5 nowych strategii rozdzielonych między dwa repozytoria:

| Repo | Bot | Strategie | Charakter |
|------|-----|-----------|-----------|
| **BotMM** | Market-Making + Quant | MM, Pairs Arb, FR Hunter, HLP Vault | Pasywne / delta-neutral |
| **BotHL** | Directional Trader | Kalman+QQE, Discord Copier, Liq Sniper | Kierunkowe / event-driven |

### Dlaczego taki podział?
- **BotMM** ma infrastrukturę do ciągłego kwotowania i monitoringu — idealne do pairs arb i FR
- **BotHL** ma connectors (HL, Binance, Bybit), position manager, exit manager — idealne do kopii sygnałów i sniping
- BotHL jest LIVE na HL (od 2026-02-11), ma gotowy system zarządzania pozycjami
- Wspólna giełda (Hyperliquid) ale oddzielne portfele/subaccounts

---

## Alokacja kapitału ($50K)

| Strategia | Repo | Kapitał | Yield/msc | APY |
|-----------|------|---------|-----------|-----|
| FR Spike Hunter | BotMM | $15K | $300-1,200 | 24-96% |
| Pairs/Stat Arb | BotMM | $15K | $300-900 | 24-72% |
| MM Bot (ETH) | BotMM | $10K | $90-140 | 11-17% |
| Discord Copier + Liq Sniper | BotHL | $10K | $500-2,000 | 60-240% |
| **TOTAL** | | **$50K** | **$1,190-4,240** | **29-102%** |

Konserwatywnie: **~$1,300/msc** (31% APY)

> HLP Vault — ON HOLD (za mało kapitału, wraca przy $100K+)

---

## BotMM — Nowe moduły (ten repo)

### MODULE 1: Pairs/Stat Arb (`bot_mm/strategies/pairs_arb.py`)

**Koncept:** Long asset A + Short asset B gdy Z-score spreadu > 2σ.
Wszystko na HL perps — delta-neutral, hedged.

#### Workplan
- [ ] 1.1 Fetch 90d hourly candles top 15 HL perps via HL API
- [ ] 1.2 Macierz korelacji + Engle-Granger cointegration test
- [ ] 1.3 Wybrać 3-5 par z p-value < 0.05
- [ ] 1.4 Spread = Price_A - Beta × Price_B (rolling OLS 30d)
- [ ] 1.5 Sygnały: Z-score (entry |Z|>2, exit Z→0, stop |Z|>3.5)
- [ ] 1.6 Egzekucja: obie nogi przez HL batch API, ALO, $5K/noga
- [ ] 1.7 Risk: max 3 pary aktywne, -$200 stop/parę, weekly cointegration recheck
- [ ] 1.8 Backtest na danych historycznych
- [ ] 1.9 Testy jednostkowe

#### Pliki
```
bot_mm/strategies/pairs_arb.py     # Spread, Z-score, sygnały, egzekucja
bot_mm/data/pair_scanner.py        # Selekcja par: korelacja, kointegracja
scripts/run_pairs_arb.py           # CLI
scripts/backtest_pairs.py          # Backtest historyczny
tests/test_pairs_arb.py
```

#### Dependency: `statsmodels>=0.14.0`

---

### MODULE 2: Funding Rate Spike Hunter (`bot_mm/strategies/fr_hunter.py`)

**Koncept:** Monitoring funding rates 50+ HL perps. Gdy |funding| > 0.05%/8h →
pozycja odwrotna (zbieramy funding od overleveraged). Exit gdy funding wraca do normy.

#### Workplan
- [ ] 2.1 Poll HL API `/info` co 60s, tracking 24h avg per asset
- [ ] 2.2 Alert: |current| > 3× avg OR > 0.05%/8h
- [ ] 2.3 Entry: SHORT gdy funding > +0.05%, LONG gdy < -0.05%
- [ ] 2.4 Filtry: volume > $10M/24h, spread < 5 bps, max 5 pozycji
- [ ] 2.5 Exit: funding < 0.01% × 3 okresy, OR -2% price stop, OR 48h max
- [ ] 2.6 PnL tracking, Discord notifications
- [ ] 2.7 Testy

#### Pliki
```
bot_mm/strategies/fr_hunter.py     # Scan, enter, manage, exit
bot_mm/data/funding_monitor.py     # Real-time funding tracker all HL perps
scripts/run_fr_hunter.py           # CLI
tests/test_fr_hunter.py
```

---

### MODULE 3: HLP Vault Manager — ON HOLD

> Odłożony — za mało kapitału. Wraca przy $100K+ (wtedy $15-20K do vault).
> Pasywny 10-25% APY, ale blokuje kapitał (4-day lockup) który jest potrzebny aktywnym strategiom.

---

## BotHL — Nowe moduły (repo: SusBot-cyber/BotHL)

### MODULE 4: Discord Copy Trader (`bot/strategies/discord_copier.py`)

**Koncept:** Odczyt sygnałów z TheLabTrading.com Discord → analiza slippage →
auto-egzekucja na HL przez istniejący HL connector BotHL.

#### Dlaczego w BotHL?
- BotHL ma `bot/exchanges/hyperliquid.py` z pełnym order/position management
- BotHL ma `bot/core/position_manager.py` z SL/TP, partial exit, breakeven
- BotHL ma `bot/core/kalman_trader.py` z logiką entry/exit — analogicznie do copiera
- BotHL ma `bot/utils/notifier.py` z Discord webhook
- Sygnały z TheLab to kierunkowe trady (LONG/SHORT) — wpasowują się w BotHL charakter

#### Workplan
- [ ] 4.1 Discord connection: `discord.py` (selfbot lub bot token z read access)
- [ ] 4.2 Signal parser — regex multi-format:
  ```
  "Buy BTC @ 65000 | SL: 64000 | TP: 67000"
  "BTC LONG $65000 5X"
  "🟢 LONG ETH Entry: 1950 Stop: 1900 Target: 2100"
  Close signals: "TP hit", "Stopped out", "Close BTC"
  ```
- [ ] 4.3 Slippage analysis:
  - < 5 bps → EXECUTE natychmiast
  - 5-20 bps → execute z adjusted entry (limit)
  - 20-50 bps → reduced size (50%)
  - > 50 bps → SKIP (za późno)
- [ ] 4.4 R:R check: (TP-entry)/(entry-SL) > 1.5 required
- [ ] 4.5 SL breach check: jeśli cena już za SL → skip
- [ ] 4.6 Execution via `HyperliquidExchange` (istniejący connector)
  - Limit order at market mid (ALO for maker)
  - Trigger SL order
  - Monitor for TP → close
- [ ] 4.7 Position tracking via `PositionManager` (istniejący)
- [ ] 4.8 Trade log: signal received → analyzed → executed/skipped (reason)
- [ ] 4.9 Safety: max 5 positions, $2K risk/trade, -$500 daily stop
- [ ] 4.10 DRY RUN mode (1 tydzień) — log bez egzekucji
- [ ] 4.11 Testy parser + copier logic

#### Pliki (w BotHL repo)
```
bot/strategies/                    # NOWY folder
bot/strategies/__init__.py
bot/strategies/discord_copier.py   # Core: listen → parse → analyze → execute
bot/data/signal_parser.py          # Regex parser multi-format
bot/data/discord_listener.py       # Discord connection + message filtering
scripts/run_discord_copier.py      # CLI runner
tests/test_signal_parser.py
tests/test_copier_logic.py
```

#### Integracja z istniejącym BotHL
```python
# discord_copier.py korzysta z:
from bot.exchanges.hyperliquid import HyperliquidExchange  # order placement
from bot.core.position_manager import PositionManager       # SL/TP management
from bot.utils.notifier import DiscordNotifier              # notifications
from bot.config import Config                               # API keys, thresholds
```

#### Dependency: `discord.py>=2.3.0`

---

### MODULE 5: Liquidation Sniper (`bot/strategies/liq_sniper.py`)

**Koncept:** Monitor dużych pozycji na HL. Gdy wieloryb blisko likwidacji →
pre-positioning by zyskać na kaskadzie.

#### Dlaczego w BotHL?
- Sniping to kierunkowy trade (LONG lub SHORT zależnie od whale pozycji)
- BotHL ma `PositionManager` z SL → bezpieczeństwo jeśli liq nie nastąpi
- BotHL ma connectors do 3 giełd → ewentualne hedging cross-exchange
- BotHL `ExitManager` z partial TP → wyjście z pozycji po kaskadzie

#### Workplan
- [ ] 5.1 Research: HL clearinghouse API — jak pobrać duże pozycje
- [ ] 5.2 Monitor: track pozycje >$1M, kalkulacja liq price
- [ ] 5.3 Alert: cena w obrębie 3% od whale liq price
- [ ] 5.4 Pre-position: $2K initial, scale to $10K w miarę zbliżania
- [ ] 5.5 Exit: close gdy volatility spada post-cascade
- [ ] 5.6 Risk: -$300/event hard stop, max 2 concurrent
- [ ] 5.7 Testy

#### Pliki (w BotHL repo)
```
bot/strategies/liq_sniper.py       # Monitor, pre-position, exit
bot/data/position_scanner.py       # HL clearinghouse position tracker
scripts/run_liq_sniper.py          # CLI
tests/test_liq_sniper.py
```

#### Challenge
HL nie eksponuje łatwo indywidualnych pozycji.
Opcje: clearinghouse state endpoint, on-chain events, WebSocket trade patterns.

---

## Architektura — Integration Points

### BotMM (ten repo) — po rozszerzeniu
```
bot_mm/
├── core/
│   ├── quoter.py          # A-S model (istniejący)
│   ├── inventory.py       # Position tracking (istniejący)
│   └── risk.py            # Risk limits (istniejący)
├── strategies/
│   ├── basic_mm.py        # Istniejący MM
│   ├── adaptive_mm.py     # Istniejący adaptive MM
│   ├── pairs_arb.py       # NOWY: Statistical arbitrage
│   ├── fr_hunter.py       # NOWY: Funding rate spike hunter
│   └── hlp_vault.py       # NOWY: HLP vault manager
├── data/
│   ├── pair_scanner.py    # NOWY: Correlation/cointegration
│   └── funding_monitor.py # NOWY: Real-time funding rates
├── exchanges/
│   └── hl_mm.py           # HL connector (istniejący)
└── ml/
    └── toxicity.py        # Toxicity detector (istniejący)
```

### BotHL (drugie repo) — po rozszerzeniu
```
bot/
├── core/
│   ├── kalman_trader.py      # Istniejący Kalman+QQE trader
│   └── position_manager.py   # Istniejący SL/TP/partial TP
├── exchanges/
│   ├── hyperliquid.py        # Istniejący HL connector
│   ├── binance.py            # Istniejący Binance connector
│   └── bybit.py              # Istniejący Bybit connector
├── strategies/               # NOWY folder
│   ├── discord_copier.py     # NOWY: Discord trade copier
│   └── liq_sniper.py         # NOWY: Liquidation sniper
├── data/
│   ├── signal_parser.py      # NOWY: Signal regex parser
│   ├── discord_listener.py   # NOWY: Discord connection
│   └── position_scanner.py   # NOWY: Large position tracker
└── utils/
    └── notifier.py           # Istniejący Discord webhook
```

---

## Priority Order

| Priorytet | Moduł | Repo | Start |
|-----------|-------|------|-------|
| 🔴 1 | FR Spike Hunter | BotMM | Natychmiast |
| 🔴 2 | Pairs/Stat Arb | BotMM | Równolegle z FR |
| 🟡 3 | Discord Copier | BotHL | Po dołączeniu do TheLab Discord |
| 🟡 4 | Liq Sniper | BotHL | Po walidacji 1-2 |
| ⏸️ — | HLP Vault | BotMM | ON HOLD (za mało kapitału) |

---

## Shared Dependencies

Oba boty:
- Hyperliquid API (mainnet)
- Python 3.11+
- Discord webhook (notifications)
- AWS t2.micro (recorder / hosting)

Nowe:
- `statsmodels>=0.14.0` (BotMM — cointegration tests)
- `discord.py>=2.3.0` (BotHL — Discord signal reader)

---

## Risk — Portfolio Level

- Max $50K total exposure across both bots
- Subaccounts na HL: osobny dla MM, osobny dla BotHL
- Daily loss limit: -$500 across all strategies → pause all
- Weekly review: APY tracking, rebalancing decision
- Korelacja strategii: MM + pairs arb are uncorrelated, FR spikes are event-driven
