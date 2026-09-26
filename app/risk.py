"""Análise de risco do portefólio. Tudo calculado por código; nada aqui decide ou envia ordens.

Os limiares são pontos de partida a validar e ajustar na Configuração.
"""

RISK_DEFAULTS = {
    "risk_conc_attn": 40, "risk_conc_high": 60,
    "risk_vol_attn": 6, "risk_vol_high": 10,
    "risk_liq_attn": 1_000_000, "risk_liq_high": 200_000,
    "risk_spread_attn": 0.3, "risk_spread_high": 1,
    "risk_young_attn": 30, "risk_young_info": 90,
    "risk_stable_attn": 0.5, "risk_stable_high": 2,
}

INFO, ATTENTION, HIGH = "info", "atenção", "alto"
RANK = {INFO: 1, ATTENTION: 2, HIGH: 3}
CRITERIA_NAMES = {
    "concentracao": "Concentração", "volatilidade": "Volatilidade", "liquidez": "Liquidez",
    "spread": "Spread", "recente": "Moeda recente", "stablecoin": "Desvio da stablecoin",
    "estado": "Estado de negociação",
}
END = " A decisão é sua."


def thresholds(cfg):
    return {k: float(cfg.get(k, v)) for k, v in RISK_DEFAULTS.items()}


def _finding(coin, criterion, severity, text):
    return {"key": f"{coin}:{criterion}", "coin": coin, "criterion": criterion,
            "severity": severity, "message": f"{coin}: {text}{END}"}


def evaluate(holdings, t):
    """Devolve a lista de riscos encontrados. Comparações estritas: o valor exatamente no limiar não dispara."""
    findings = []
    for h in holdings:
        coin = h["coin"]
        if not h.get("priced") or h.get("dust"):
            continue  # sem preço ou poeira: sem análise, sem ruído
        if h.get("stable"):
            dev = h.get("stable_dev_pct")
            if dev is not None and dev > t["risk_stable_attn"]:
                sev = HIGH if dev > t["risk_stable_high"] else ATTENTION
                findings.append(_finding(coin, "stablecoin", sev,
                    f"o preço desviou-se {dev:.2f}% de 1 USDT. Uma stablecoin que perde a paridade pode não recuperar."))
            continue
        w = h["weight"]
        if w > t["risk_conc_attn"]:
            sev = HIGH if w > t["risk_conc_high"] else ATTENTION
            findings.append(_finding(coin, "concentracao", sev,
                f"representa {w:.1f}% do teu portefólio. Muito peso numa só moeda faz uma queda dela pesar em tudo."))
        vol = h.get("vol30")
        if vol is not None and vol > t["risk_vol_attn"]:
            sev = HIGH if vol > t["risk_vol_high"] else ATTENTION
            findings.append(_finding(coin, "volatilidade", sev,
                f"variou em média {vol:.1f}% por dia nos últimos 30 dias. Oscila muito, o que aumenta o risco de perdas rápidas."))
        liq = h.get("quote_volume")
        if liq is not None and liq < t["risk_liq_attn"]:
            sev = HIGH if liq < t["risk_liq_high"] else ATTENTION
            findings.append(_finding(coin, "liquidez", sev,
                f"negociou apenas {liq:,.0f} USDT nas últimas 24 h. Com pouco volume pode ser difícil vender ao preço esperado."))
        sp = h.get("spread_pct")
        if sp is not None and sp > t["risk_spread_attn"]:
            sev = HIGH if sp > t["risk_spread_high"] else ATTENTION
            findings.append(_finding(coin, "spread", sev,
                f"a diferença entre compra e venda é {sp:.2f}%. Vender custa logo essa diferença."))
        age = h.get("age_days")
        if age is not None:
            if age < t["risk_young_attn"]:
                findings.append(_finding(coin, "recente", ATTENTION,
                    f"só tem {age} dias de histórico. Moedas novas têm comportamento pouco conhecido."))
            elif age < t["risk_young_info"]:
                findings.append(_finding(coin, "recente", INFO,
                    f"tem menos de {int(t['risk_young_info'])} dias de histórico ({age} dias)."))
        st = h.get("status")
        if st is not None and st != "TRADING":
            findings.append(_finding(coin, "estado", HIGH,
                f"o par não está em negociação normal na Binance (estado: {st}). Pode estar em pausa ou a ser retirado."))
    return findings
