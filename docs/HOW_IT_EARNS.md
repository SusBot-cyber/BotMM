# How BotMM Earns — Market Making Profit Flow

## TL;DR

Bot zarabia na **spread capture** — kupuje tanio, sprzedaje drogo, powtarza setki razy dziennie. Każdy round-trip zarabia ~$0.60 netto po fee. Przy 198 fillach dziennie = **$120/dzień z $50K kapitału**.

---

## Krok po kroku: 1 dzień na ETH ($12.5K kapitał)

### 1. Ustawienie kwotowań

ETH = $3,000. Bot liczy spread z modelu Avellaneda-Stoikov:
- Volatility (ATR) = 0.8% → spread = 1.5 bps × 1.5 (vol_multiplier) = **~$0.68 na stronę**
- Bot ustawia:
  - **BID** (kupno): $2,999.32 × $150 (size)
  - **ASK** (sprzedaż): $3,000.68 × $150

### 2. Round-trip (jeden zarobek)

1. Ktoś sprzedaje na rynku → trafia w nasz BID → **kupujemy ETH za $2,999.32**
2. 30 min później ktoś kupuje → trafia w nasz ASK → **sprzedajemy za $3,000.68**
3. **Gross profit = $1.36 na $150 pozycji**
4. Fee: $150 × 0.015% × 2 strony = **-$0.045**
5. **Net profit tego round-trip = ~$1.31**

### 3. Skala — ile razy dziennie?

Bot nie czeka na idealne round-tripy. Na $12.5K kapitał:
- Wystawia 2 levele po $150 = **$600 na stronę** (bid+ask)
- Średnio **~50 filli dziennie** per asset (72,142 filli / 365 dni / 4 assety)
- Nie wszystkie to idealne round-tripy — część to wypełnienia jednej strony, po czym cena wraca i druga strona łapie

### 4. Zarządzanie inventory

Po kupnie ETH bot ma **dodatni inventory** (jest long). Co robi:
- **Inventory skew** (0.3): przesuwa kwotowania — ASK bliżej mid (łatwiej sprzedać), BID dalej (trudniej kupić więcej)
- **Kalman+QQE bias**: jeśli trend UP → trzyma dłużej, jeśli DOWN → agresywniej sprzedaje
- **Toxicity detector**: jeśli po kupnie cena spada (adverse selection) → rozszerza spread (ochrona)

### 5. Bilans dnia (ETH)

- **~25 round-tripów** × ~$1.30 net = **~$33 gross**
- Minus: kilka pozycji zamkniętych ze stratą (cena uciekła) = **-$5**
- Minus: fee per fill × 50 fills = **-$11**
- **Dziennie netto: ~$33** (= $11,969 / 365d)
- **77% dni jest zyskownych**

---

## Różnice między assetami

| Cecha           |     BTC |     ETH |     SOL |     XRP |
|-----------------|---------|---------|---------|---------|
| Base spread     | 2.0 bps | 1.5 bps | 1.5 bps | 1.5 bps |
| Volatility      |   ~0.5% |   ~0.8% |   ~0.7% |   ~0.7% |
| Fills/dzień     |      42 |      51 |      53 |      52 |
| Net PnL/dzień   |  $24.88 |  $32.79 |  $29.65 |  $32.84 |
| Sharpe          |    11.0 |     9.0 |    10.7 |     9.1 |
| Profitable days |     80% |     77% |     79% |     76% |
| Compound        |      ON |      ON |     OFF |     OFF |

**BTC** — najstabilniejszy (Sharpe 11, 80% profitable), ale zarabia najmniej (niska vol).
**ETH** — najlepszy growth engine (highest gross), ale większa wariancja.
**SOL/XRP** — fixed capital, stabilne zarobki.

---

## Skąd bierze się edge?

Bot zarabia na **mikrostrukturze rynku** — nie zgaduje kierunku ceny.

### Spread jako opłata za płynność
Traderzy, którzy chcą natychmiast kupić/sprzedać, płacą spread. Bot jest "pośrednikiem" — dostarcza płynność i pobiera za to opłatę.

### Dlaczego spread > fee?
- Efektywny spread to często **5-8 bps** (nie minimum 3 bps), bo:
  - Volatilność rośnie → Avellaneda-Stoikov dynamicznie rozszerza spread
  - Inventory rośnie → skew dodaje offset
  - Toxicity wysoka → 1.5× spread
- Fee to stałe **1.5 bps** (0.015% per stronę)
- **Net margin: ~3.5-6.5 bps per round-trip**

### Kluczowa nierówność

> **Spread capture (~5-8 bps) > Fee cost (~3 bps round-trip) = NET PROFIT**

Na $150 pozycji:
- Gross: $150 × 0.0006 = **$0.09 per trip**
- Fee: $150 × 0.0003 = **-$0.045**
- Net: **$0.045 per trip × ~200 tripów/dzień = $9/dzień na $1K**

---

## Fee structure

### Hyperliquid fee tiers (base)
- **Maker: +0.015%** (koszt, NIE rebate na base tier)
- **Taker: +0.045%**
- Rebate dopiero przy >$500M 14d volume

### Impact na PnL
```
Gross PnL (365d, $50K):     $85,380
Fees (26.6% of gross):    -$22,710
Net PnL:                    $62,670
```

Fee zjada **26.6% gross profitu** — to stały koszt biznesowy. Bot zarabia netto bo spread capture > fee cost.

### HYPE Staking rabaty

| Rabat   | HYPE stake | Koszt ($30/HYPE) | Oszcz./rok | Te $ w bocie | Opłaca się?          |
|---------|------------|------------------|------------|--------------|----------------------|
| 0%      |          0 |               $0 |         $0 |           $0 | —                    |
| **5%**  |     **10** |         **$300** | **$1,136** |     **$376** | **✅ TAK (+$760)**   |
| 10%     |        100 |           $3,000 |     $2,271 |       $3,759 | ❌ NIE (-$1,488)     |
| 15%     |      1,000 |          $30,000 |     $3,407 |      $37,590 | ❌ NIE               |
| 20%     |     10,000 |         $300,000 |     $4,542 |     $375,900 | ❌ NIE               |

**Jedyne co się opłaca przy $50K kapitału to 10 HYPE ($300) → 5% rabat.** Każdy wyższy tier: koszt stake'a rośnie 10× a rabat tylko +5pp. Bot zarabia 125%/rok, więc kapitał w bocie > kapitał zamrożony w stake.

**Próg opłacalności 100 HYPE stake:** kapitał bota > ~$83K (wtedy fee rośnie szybciej niż marginalny zysk).

---

## Compound effect

BTC i ETH mają compound ON — zysk jest reinwestowany:

```
Dzień 1:    $12,500 alokacji → zarabia ~$25/dzień
Dzień 180:  $16,000 effective → zarabia ~$32/dzień  
Dzień 365:  $30,000+ effective → zarabia ~$48/dzień
```

Compound podwaja effective capital na BTC/ETH w ciągu roku. SOL/XRP zostają na fixed $12,500 — stabilne ale bez growth.

---

## Supervisor V3 effect

Supervisor monitoruje performance i łagodnie przesuwa kapitał:
- Window 45d, cut 3-10%, 1% daily mean-revert
- Chroni przed prolongowanym losem na jednym assecie
- Nie niszczy alokacji (min $5K per asset)
- **+21.4% nad EQUAL** ($62.7K vs $51.6K)
