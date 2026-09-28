"""Ordens na Binance TESTNET. Único módulo do projeto que envia ordens (POST) ou as cancela (DELETE).

Só fala com https://testnet.binance.vision: qualquer outro endereço faz falhar a construção. Recebe as chaves como
argumentos e nunca lê ficheiros de chaves (nem importa o módulo que os guarda): não tem como abrir a chave real.
"""
import hashlib
import hmac
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

from . import netmeter

TESTNET_BASE = "https://testnet.binance.vision"

ERRORS_PT = {
    -2015: "A Testnet recusou a chave. Confirma a chave e o segredo da Testnet (são diferentes dos da conta real).",
    -2014: "O formato da chave da Testnet não é válido. Copia-a de novo, sem espaços.",
    -1022: "A assinatura não foi aceite. Confirma o segredo da Testnet.",
    -1021: "O relógio deste computador está desajustado em relação à Binance. Sincroniza a hora.",
    -1003: "A Testnet limitou os pedidos por agora. Tenta daqui a um minuto.",
    -1013: "A ordem foi recusada por violar um filtro do mercado (preço, quantidade ou valor mínimo).",
    -1111: "A ordem tem casas decimais a mais para este par.",
    -2010: "A Testnet recusou a ordem (saldo insuficiente ou ordem repetida).",
    -2011: "A ordem a cancelar não existe na Testnet.",
    -2013: "A ordem não existe na Testnet.",
}
UNKNOWN_ORDER = (-2011, -2013)
DUPLICATE = -2010
FATAL_CODES = (-2015, -2014, -1022)          # chaves recusadas: o bot pára em segurança (o relógio corrige-se sozinho)
_OFFSET = {"ms": 0}                            # diferença entre o relógio local e o da Binance (erro -1021)


class TraderError(Exception):
    """Erro já em português simples. `code` é o código da Binance (None = sem ligação)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _decimals(step):
    if not step:
        return 8
    return max(0, -Decimal(str(step)).normalize().as_tuple().exponent)


def fmt(x, step=None):
    """Número em texto sem notação científica e com as casas decimais do passo do par."""
    d = _decimals(step)
    return format(Decimal(str(x)).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP), "f")


class Trader:
    def __init__(self, key="", secret="", base=TESTNET_BASE, opener=urllib.request.urlopen):
        if base != TESTNET_BASE:
            raise TraderError("Este módulo só pode falar com a Testnet da Binance.")
        self._key, self._secret, self._base, self._open = key, secret, base, opener

    @property
    def has_keys(self):
        return bool(self._key and self._secret)

    def _call(self, method, path, params=None, signed=False):
        try:
            return self._once(method, path, params, signed)
        except TraderError as exc:
            if exc.code != -1021 or not signed:
                raise
            # relógio desajustado (comum no Pi ao arrancar): mede a diferença para a Binance e tenta uma vez mais
            server = int(self._once("GET", "/api/v3/time", None, False)["serverTime"])
            _OFFSET["ms"] = server - int(time.time() * 1000)
            return self._once(method, path, params, signed)

    def _once(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.has_keys:
                raise TraderError("Faltam as chaves da Testnet.", -2015)
            params["timestamp"] = int(time.time() * 1000) + _OFFSET["ms"]
            params["recvWindow"] = 10000
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self._key
        url = f"{self._base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers=headers, method=method)
        t0 = time.monotonic()
        try:
            with self._open(request, timeout=8) as resp:
                data = json.load(resp)
            netmeter.record((time.monotonic() - t0) * 1000)
            return data
        except urllib.error.HTTPError as exc:
            netmeter.record((time.monotonic() - t0) * 1000, error=exc.code == 429 or exc.code >= 500)
            code, msg = None, ""
            try:
                body = json.loads(exc.read().decode())
                code, msg = body.get("code"), body.get("msg", "")
            except Exception:
                pass
            if exc.code == 429:
                code = -1003
            raise TraderError(ERRORS_PT.get(code, f"A Testnet respondeu com erro ({exc.code}). {msg}".strip()),
                              code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            netmeter.record((time.monotonic() - t0) * 1000, error=True, timeout=True)
            raise TraderError("Sem ligação à Testnet.") from None

    # ---- dados públicos (a Testnet é também a fonte de preços dos bots em modo testnet) ----
    def klines_1m(self, symbol, start_ms=None, limit=1000):
        params = {"symbol": symbol, "interval": "1m", "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        return self._call("GET", "/api/v3/klines", params)

    def ticker24(self, symbol):
        return self._call("GET", "/api/v3/ticker/24hr", {"symbol": symbol})

    def symbol_rules(self, symbol):
        """Regras do par NA TESTNET (podem diferir do real)."""
        data = self._call("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        info = data["symbols"][0]
        rules = {"tick": None, "step": None, "min_notional": 5.0, "min_qty": 0.0, "max_qty": None,
                 "status": info.get("status")}
        for f in info.get("filters", []):
            kind = f["filterType"]
            if kind == "PRICE_FILTER":
                rules["tick"] = float(f["tickSize"])
            elif kind == "LOT_SIZE":
                rules["step"] = float(f["stepSize"])
                rules["min_qty"] = float(f.get("minQty", 0))
                rules["max_qty"] = float(f["maxQty"]) if f.get("maxQty") else None
            elif kind in ("NOTIONAL", "MIN_NOTIONAL"):
                rules["min_notional"] = float(f.get("minNotional", rules["min_notional"]))
        return rules

    def trading_symbols(self):
        """Pares que existem e estão a negociar na Testnet."""
        data = self._call("GET", "/api/v3/exchangeInfo")
        return {s["symbol"] for s in data.get("symbols", []) if s.get("status") == "TRADING"}

    def server_time(self):
        return int(self._call("GET", "/api/v3/time")["serverTime"])

    # ---- conta e ordens (assinado) ----
    def account(self):
        return self._call("GET", "/api/v3/account", signed=True).get("balances", [])

    def free_balance(self, asset):
        for b in self.account():
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def open_orders(self, symbol):
        return self._call("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def get_order(self, symbol, cid):
        return self._call("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": cid}, signed=True)

    def order_trades(self, symbol, order_id):
        return self._call("GET", "/api/v3/myTrades", {"symbol": symbol, "orderId": order_id}, signed=True)

    def all_orders(self, symbol, start_ms=None, limit=1000):
        """Ordens do par (abertas e fechadas, de todos os bots da conta): só para a recuperação. Peso 20 (a confirmar)."""
        params = {"symbol": symbol, "limit": limit}
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        return self._call("GET", "/api/v3/allOrders", params, signed=True)

    def place_limit(self, symbol, side, qty, price, cid, tick=None, step=None):
        return self._call("POST", "/api/v3/order", {
            "symbol": symbol, "side": side.upper(), "type": "LIMIT", "timeInForce": "GTC",
            "quantity": fmt(qty, step), "price": fmt(price, tick), "newClientOrderId": cid,
            "newOrderRespType": "ACK"}, signed=True)

    def place_market(self, symbol, side, qty, cid, step=None):
        return self._call("POST", "/api/v3/order", {
            "symbol": symbol, "side": side.upper(), "type": "MARKET", "quantity": fmt(qty, step),
            "newClientOrderId": cid, "newOrderRespType": "FULL"}, signed=True)

    def cancel_order(self, symbol, cid):
        return self._call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": cid}, signed=True)
