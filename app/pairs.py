"""Escolha do par por atividade. O código filtra e pontua; o texto só explica. O utilizador aprova."""

MEMECOINS = {"DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "MEME", "DOGS", "HMSTR", "CATI", "NOT", "TURBO",
             "BOME", "PEOPLE", "MEW", "POPCAT", "NEIRO", "PNUT", "GOAT", "MOODENG", "ACT", "1000SATS", "1000CAT"}
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "USD1", "AEUR", "XUSD", "USDE"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

# pontos de partida a validar
MIN_QUOTE_VOLUME = 20_000_000   # USDT em 24 h: liquidez alta
MAX_SPREAD_PCT = 0.05           # spread apertado
RANGE_MIN, RANGE_MAX, RANGE_TARGET = 2.0, 8.0, 4.0   # movimento em 24 h: moderado


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def suggest(tickers, exclusions=("memecoins", "leveraged", "stables"), limit=5):
    out = []
    for sym, t in tickers.items():
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if "stables" in exclusions and base in STABLE_BASES:
            continue
        if "memecoins" in exclusions and base in MEMECOINS:
            continue
        if "leveraged" in exclusions and base.endswith(LEVERAGED_SUFFIXES) and len(base) > 4:
            continue
        last, high, low = _f(t.get("lastPrice")), _f(t.get("highPrice")), _f(t.get("lowPrice"))
        bid, ask, vol = _f(t.get("bidPrice")), _f(t.get("askPrice")), _f(t.get("quoteVolume"))
        if not (last > 0 and bid > 0 and ask > 0 and high >= low > 0):
            continue
        spread = (ask - bid) / bid * 100
        rng = (high - low) / last * 100
        if vol < MIN_QUOTE_VOLUME or spread > MAX_SPREAD_PCT or not RANGE_MIN <= rng <= RANGE_MAX:
            continue
        # pontuação: mais liquidez, spread menor, movimento perto do alvo
        score = min(vol / 200_000_000, 1.0) * 40 + (1 - spread / MAX_SPREAD_PCT) * 20 \
            + (1 - abs(rng - RANGE_TARGET) / (RANGE_MAX - RANGE_MIN)) * 40
        out.append({"pair": sym, "base": base, "price": last, "quote_volume": vol, "spread_pct": spread,
                    "range_pct": rng, "score": score,
                    "why": [f"volume de {vol / 1e6:.0f} M USDT em 24 h (liquidez alta)",
                            f"spread de {spread:.3f}% (apertado)",
                            f"moveu-se {rng:.1f}% nas últimas 24 h (moderado: nem parado, nem louco)"]})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def for_testnet(found, testnet_symbols, fallback="BTCUSDT"):
    """Só pares que existem na Testnet. Se nenhum sugerido lá existir, plano B: um par fixo e líquido."""
    ok = [x for x in found if x["pair"] in testnet_symbols]
    if ok:
        return ok
    if fallback in testnet_symbols:
        return [{"pair": fallback, "base": fallback[:-4], "price": 0.0, "quote_volume": 0.0, "spread_pct": 0.0,
                 "range_pct": 0.0, "score": 0.0,
                 "why": ["plano B: nenhum par sugerido existe na Testnet; BTCUSDT é líquido e serve para testar "
                         "o mecanismo de ordens"]}]
    return []
