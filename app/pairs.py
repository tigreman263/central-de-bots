"""Escolha do par por atividade. O código filtra e pontua; o texto só explica. O utilizador aprova.

Os critérios (volume, spread, amplitude em 24 h) são configuráveis (Configuração > Sugestões de moedas). Como o mercado muda,
pode acontecer que nenhum par os cumpra: então (se a adaptação estiver ligada) alargam-se por degraus, dizendo-o, e em
último caso mostram-se os pares mais próximos, marcados como fora dos critérios. Nunca fica em branco por causa dos números.
"""

MEMECOINS = {"DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF", "MEME", "DOGS", "HMSTR", "CATI", "NOT", "TURBO",
             "BOME", "PEOPLE", "MEW", "POPCAT", "NEIRO", "PNUT", "GOAT", "MOODENG", "ACT", "1000SATS", "1000CAT"}
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "USD1", "AEUR", "XUSD", "USDE"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

# pontos de partida a validar (são os valores por defeito das definições; o volume está em milhões de USDT)
DEFAULTS = {"pair_min_volume": "20", "pair_max_spread": "0.05", "pair_range_min": "2", "pair_range_max": "8",
            "pair_range_target": "4", "pair_adaptive": "1"}
# degraus de alargamento quando nada cumpre: (volume ×, spread ×, amplitude mínima ×, amplitude máxima ×)
RELAX = [(1.0, 1.0, 1.0, 1.0), (0.5, 1.5, 0.5, 1.5), (0.2, 3.0, 0.25, 2.5)]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def params_from(cfg):
    """Critérios a partir das definições (texto). Valores inválidos voltam ao ponto de partida."""
    def num(key):
        try:
            return float(cfg.get(key, DEFAULTS[key]))
        except (TypeError, ValueError):
            return float(DEFAULTS[key])
    return {"volume": num("pair_min_volume") * 1e6, "spread": num("pair_max_spread"), "range_min": num("pair_range_min"),
            "range_max": num("pair_range_max"), "range_target": num("pair_range_target")}


def _universe(tickers, exclusions, only=None):
    for sym, t in tickers.items():
        if not sym.endswith("USDT") or (only is not None and sym not in only):
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
        yield {"pair": sym, "base": base, "price": last, "quote_volume": vol,
               "spread_pct": (ask - bid) / bid * 100, "range_pct": (high - low) / last * 100}


def _limits(p, level):
    k = RELAX[level]
    return {"volume": p["volume"] * k[0], "spread": p["spread"] * k[1], "lo": p["range_min"] * k[2], "hi": p["range_max"] * k[3]}


def _failed(c, lim):
    """O que este par não cumpre, em texto (vazio = cumpre tudo)."""
    out = []
    if c["quote_volume"] < lim["volume"]:
        out.append(f"volume de {c['quote_volume'] / 1e6:.1f} M USDT (pedido: {lim['volume'] / 1e6:g} M ou mais)")
    if c["spread_pct"] > lim["spread"]:
        out.append(f"spread de {c['spread_pct']:.3f}% (pedido: até {lim['spread']:.3g}%)")
    if not lim["lo"] <= c["range_pct"] <= lim["hi"]:
        out.append(f"moveu-se {c['range_pct']:.1f}% em 24 h (pedido: {lim['lo']:.3g}% a {lim['hi']:.3g}%)")
    return out


def _score(c, p):
    """Mais liquidez, spread menor, movimento perto do alvo (0 a 100)."""
    span = max(p["range_max"] - p["range_min"], 1e-9)
    return (min(c["quote_volume"] / (10 * p["volume"]), 1.0) * 40
            + max(0.0, 1 - c["spread_pct"] / p["spread"]) * 20
            + max(0.0, 1 - abs(c["range_pct"] - p["range_target"]) / span) * 40)


def _item(c, p, failed):
    if failed:
        why = ["Fora dos teus critérios: " + "; ".join(failed),
               f"volume de {c['quote_volume'] / 1e6:.0f} M USDT, spread de {c['spread_pct']:.3f}%, "
               f"moveu-se {c['range_pct']:.1f}% nas últimas 24 h"]
    else:
        why = [f"volume de {c['quote_volume'] / 1e6:.0f} M USDT em 24 h (liquidez alta)",
               f"spread de {c['spread_pct']:.3f}% (apertado)",
               f"moveu-se {c['range_pct']:.1f}% nas últimas 24 h (moderado: nem parado, nem louco)"]
    return {**c, "score": _score(c, p), "why": why, "meets": not failed, "failed": failed}


def suggest(tickers, exclusions=("memecoins", "leveraged", "stables"), limit=5, params=None, only=None):
    """Só os pares que cumprem TODOS os critérios (sem alargar), do melhor para o pior."""
    p = params or params_from(DEFAULTS)
    lim = _limits(p, 0)
    out = [_item(c, p, []) for c in _universe(tickers, exclusions, only) if not _failed(c, lim)]
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def describe(p, level=0):
    lim = _limits(p, level)
    return (f"volume acima de {lim['volume'] / 1e6:g} M USDT, spread até {lim['spread']:.3g}% e movimento de "
            f"{lim['lo']:.3g}% a {lim['hi']:.3g}% em 24 h")


def select(tickers, exclusions=("memecoins", "leveraged", "stables"), params=None, only=None, limit=5, adaptive=True):
    """Sugestões com plano B: {"items", "level", "note", "criteria"}.

    level 0: cumprem os critérios. 1 e 2: nenhum cumpria; critérios alargados por degraus (só com `adaptive`).
    3: nem alargados; os pares mais próximos, marcados. Sem pares lidos: lista vazia (não se inventa nada).
    """
    p = params or params_from(DEFAULTS)
    universe = list(_universe(tickers, exclusions, only))
    criteria = describe(p)
    if not universe:
        return {"items": [], "level": 0, "note": "", "criteria": criteria}
    strict = _limits(p, 0)
    for level in range(3 if adaptive else 1):
        lim = _limits(p, level)
        ok = [c for c in universe if not _failed(c, lim)]
        if ok:
            items = sorted((_item(c, p, _failed(c, strict)) for c in ok), key=lambda x: (not x["meets"], -x["score"]))[:limit]
            note = "" if level == 0 else (
                f"Nenhum par cumpria os teus critérios ({criteria}). Alarguei-os para poderes escolher: {describe(p, level)}. "
                "Os pares marcados estão fora dos critérios que definiste.")
            return {"items": items, "level": level, "note": note, "criteria": criteria}
    if not adaptive:
        return {"items": [], "level": 0, "criteria": criteria,
                "note": f"Nenhum par cumpre os critérios atuais ({criteria}). Alarga-os na Configuração ou liga a opção de "
                        "alargar automaticamente."}

    def gap(c):
        return (max(0.0, 1 - c["quote_volume"] / strict["volume"]) + max(0.0, c["spread_pct"] / strict["spread"] - 1)
                + max(0.0, strict["lo"] - c["range_pct"]) / strict["lo"] + max(0.0, c["range_pct"] - strict["hi"]) / strict["hi"])
    near = sorted((c for c in universe if c["quote_volume"] > 0), key=gap)[:limit]
    return {"items": [_item(c, p, _failed(c, strict)) for c in near], "level": 3, "criteria": criteria,
            "note": f"Nenhum par cumpre os teus critérios ({criteria}), nem alargados. Mostro os mais próximos, todos marcados "
                    "como fora dos critérios: escolhe com cuidado ou revê os critérios na Configuração."}


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
