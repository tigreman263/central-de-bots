"""Leitura da Binance (só leitura). Este módulo só faz pedidos GET.

Não existe aqui nenhuma função de ordens, vendas, transferências ou levantamentos.
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.binance.com"

# Permissões da chave que a tornam perigosa: qualquer uma destas ligada => a chave é recusada.
DANGEROUS_FLAGS = {
    "enableWithdrawals": "levantamentos",
    "enableSpotAndMarginTrading": "trading spot e margem",
    "enableMargin": "margem",
    "enableFutures": "futuros",
    "enableInternalTransfer": "transferências internas",
    "permitsUniversalTransfer": "transferências universais",
    "enableVanillaOptions": "opções",
}

ERRORS_PT = {
    -2015: "A Binance recusou a chave. Confirma a chave, o segredo e se o IP desta máquina está na lista permitida.",
    -2014: "O formato da chave não é válido. Copia-a de novo, sem espaços.",
    -1022: "A assinatura não foi aceite. Confirma o segredo (a segunda chave).",
    -1021: "O relógio deste computador está desajustado em relação à Binance. Sincroniza a hora.",
    -1003: "A Binance limitou os pedidos por agora. Tenta daqui a um minuto.",
}


class BinanceError(Exception):
    """Erro já em português simples, seguro para mostrar ao utilizador."""


def check_permissions(restrictions):
    """Devolve (ok, problemas, avisos) a partir de /sapi/v1/account/apiRestrictions."""
    problems = []
    if not restrictions.get("enableReading"):
        problems.append("a chave não tem permissão de leitura")
    for flag, label in DANGEROUS_FLAGS.items():
        if restrictions.get(flag):
            problems.append(f"a chave tem permissão de {label}")
    warnings = []
    if not restrictions.get("ipRestrict"):
        warnings.append("A chave não está restrita a um IP. Na Binance, restringe-a ao IP da tua casa.")
    return (not problems, problems, warnings)


class BinanceReader:
    def __init__(self, key="", secret="", base=BASE, opener=urllib.request.urlopen):
        self._key, self._secret, self._base, self._open = key, secret, base, opener

    def _get(self, path, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self._key
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._open(request, timeout=8) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read().decode()).get("code")
            except Exception:
                code = None
            if exc.code == 429:
                code = -1003
            raise BinanceError(ERRORS_PT.get(code, f"A Binance respondeu com erro ({exc.code}).")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BinanceError("Sem ligação à Binance.") from None

    # ---- privado (chave só de leitura) ----
    def account(self):
        data = self._get("/api/v3/account", signed=True)
        return [b for b in data.get("balances", [])]

    def restrictions(self):
        return self._get("/sapi/v1/account/apiRestrictions", signed=True)

    def _paged(self, path, pages=10):
        rows, page = [], 1
        while page <= pages:
            data = self._get(path, {"size": 100, "current": page}, signed=True)
            rows += data.get("rows", [])
            if len(rows) >= int(data.get("total", 0)) or not data.get("rows"):
                break
            page += 1
        return rows

    # Simple Earn (leitura): posições e produtos disponíveis
    def earn_flexible_positions(self):
        return self._paged("/sapi/v1/simple-earn/flexible/position")

    def earn_locked_positions(self):
        return self._paged("/sapi/v1/simple-earn/locked/position")

    def earn_flexible_products(self):
        return self._paged("/sapi/v1/simple-earn/flexible/list", pages=6)

    def earn_locked_products(self):
        return self._paged("/sapi/v1/simple-earn/locked/list", pages=4)

    # ---- público ----
    def ticker24(self, symbol):
        return self._get("/api/v3/ticker/24hr", {"symbol": symbol})

    def tickers_all(self):
        """Todos os pares numa só chamada pública: {símbolo: ticker}."""
        return {t["symbol"]: t for t in self._get("/api/v3/ticker/24hr")}

    def klines(self, symbol, limit=90):
        return self._get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": limit})

    def klines_1m(self, symbol, start_ms=None, limit=1000):
        params = {"symbol": symbol, "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        return self._get("/api/v3/klines", params)

    def symbol_rules(self, symbol):
        """Regras do par: passo de preço, passo de quantidade e ordem mínima (em USDT)."""
        data = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        info = data["symbols"][0]
        rules = {"tick": None, "step": None, "min_notional": 5.0, "status": info.get("status")}
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                rules["tick"] = float(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                rules["step"] = float(f["stepSize"])
            elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
                rules["min_notional"] = float(f.get("minNotional", rules["min_notional"]))
        return rules

    def symbol_status(self, symbol):
        data = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = data.get("symbols", [])
        return symbols[0]["status"] if symbols else None
