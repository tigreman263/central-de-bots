"""Validação da configuração: devolve valores limpos e erros em português simples."""

EXCLUSION_CATEGORIES = {
    "memecoins": "Memecoins",
    "leveraged": "Moedas alavancadas",
    "stables": "Stablecoins fora da caixa",
}

LABELS = {
    "capital_eur": "Capital simulado (EUR)",
    "split_reserve": "Reserva (%)",
    "split_trading": "Trading (%)",
    "split_cash": "Caixa (%)",
    "max_loss_trade": "Perda máxima por operação (%)",
    "profit_to_reserve": "Lucro para a reserva (%)",
    "pause_drawdown": "Pausa se o trading cair (%)",
    "exclusions": "Categorias a evitar",
    "pair_min_volume": "Pares: volume mínimo em 24 h (M USDT)",
    "pair_max_spread": "Pares: spread máximo (%)",
    "pair_range_min": "Pares: movimento mínimo em 24 h (%)",
    "pair_range_max": "Pares: movimento máximo em 24 h (%)",
    "pair_range_target": "Pares: movimento ideal (%)",
    "pair_adaptive": "Pares: alargar os critérios se nenhum par cumprir",
}
PAIR_FIELDS = [("pair_min_volume", 0.1, 10_000), ("pair_max_spread", 0.001, 2), ("pair_range_min", 0.1, 30),
               ("pair_range_max", 0.5, 100), ("pair_range_target", 0.1, 100)]


RISK_PAIRS = [  # (rótulo, chave "atenção", chave "alto", unidade, alto é maior?)
    ("Concentração de uma moeda", "risk_conc_attn", "risk_conc_high", "%", True),
    ("Volatilidade diária (30 dias)", "risk_vol_attn", "risk_vol_high", "%", True),
    ("Liquidez (volume 24 h)", "risk_liq_attn", "risk_liq_high", "USDT", False),
    ("Spread", "risk_spread_attn", "risk_spread_high", "%", True),
    ("Desvio da stablecoin", "risk_stable_attn", "risk_stable_high", "%", True),
]
for _label, _a, _h, _u, _ in RISK_PAIRS:
    LABELS[_a] = f"{_label}: atenção"
    LABELS[_h] = f"{_label}: alto"
LABELS["risk_young_attn"] = "Moeda recente: atenção (dias)"
LABELS["risk_young_info"] = "Moeda recente: info (dias)"


def _number(form, key, low, high, errors):
    raw = (form.get(key) or "").replace(",", ".").strip()
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{LABELS[key]}: escreve um número.")
        return None
    if not low <= value <= high:
        errors.append(f"{LABELS[key]}: tem de estar entre {low:g} e {high:g}.")
        return None
    return value


def _clean(value):
    return f"{value:g}"


def parse_config(form):
    errors = []
    values = {}
    for key, low, high in [
        ("capital_eur", 1, 10_000_000),
        ("split_reserve", 0, 100),
        ("split_trading", 0, 100),
        ("split_cash", 0, 100),
        ("max_loss_trade", 0.5, 5),
        ("profit_to_reserve", 0, 100),
        ("pause_drawdown", 5, 50),
    ]:
        v = _number(form, key, low, high, errors)
        if v is not None:
            values[key] = v
    for label, a, h, unit, high_bigger in RISK_PAIRS:
        for key in (a, h):
            if key in form:
                v = _number(form, key, 0.01, 1_000_000_000, errors)
                if v is not None:
                    values[key] = v
        if a in values and h in values and (values[h] <= values[a] if high_bigger else values[h] >= values[a]):
            errors.append(f"{label}: o limiar \"alto\" tem de ser {'maior' if high_bigger else 'menor'} que o de atenção.")
    for key in ("risk_young_attn", "risk_young_info"):
        if key in form:
            v = _number(form, key, 1, 3650, errors)
            if v is not None:
                values[key] = v
    if "risk_young_attn" in values and "risk_young_info" in values and values["risk_young_info"] <= values["risk_young_attn"]:
        errors.append("Moeda recente: o limiar de info tem de ser maior que o de atenção.")
    for key, low, high in PAIR_FIELDS:
        if key in form:
            v = _number(form, key, low, high, errors)
            if v is not None:
                values[key] = v
    if "pair_range_min" in values and "pair_range_max" in values and values["pair_range_max"] <= values["pair_range_min"]:
        errors.append("Pares: o movimento máximo tem de ser maior que o mínimo.")
    elif all(k in values for k in ("pair_range_min", "pair_range_max", "pair_range_target")) and \
            not values["pair_range_min"] <= values["pair_range_target"] <= values["pair_range_max"]:
        errors.append("Pares: o movimento ideal tem de estar entre o mínimo e o máximo.")
    split_keys = ("split_reserve", "split_trading", "split_cash")
    if all(k in values for k in split_keys):
        total = sum(values[k] for k in split_keys)
        if abs(total - 100) > 1e-9:
            errors.append(f"A divisão do saldo tem de dar 100% (agora dá {total:g}%).")
    chosen = [c for c in form.getlist("exclusions") if c in EXCLUSION_CATEGORIES]
    if errors:
        return None, errors
    clean = {k: _clean(v) for k, v in values.items()}
    clean["exclusions"] = ",".join(chosen)
    if "pair_adaptive_sent" in form or "pair_adaptive" in form:      # a caixa desmarcada não vai no formulário
        clean["pair_adaptive"] = "1" if form.get("pair_adaptive") == "1" else "0"
    return clean, []


def describe_changes(old, new):
    """Lista (rótulo, antes, depois) só do que muda."""
    changes = []
    for key, label in LABELS.items():
        if key not in new:
            continue
        before, after = old.get(key, ""), new.get(key, "")
        if key == "exclusions":
            before = _names(before)
            after = _names(after)
        elif key == "pair_adaptive":
            before, after = ("sim" if before != "0" else "não"), ("sim" if after != "0" else "não")
        if before != after:
            changes.append((label, before or "nenhuma", after or "nenhuma"))
    return changes


def _names(csv):
    return ", ".join(EXCLUSION_CATEGORIES[c] for c in csv.split(",") if c in EXCLUSION_CATEGORIES)
