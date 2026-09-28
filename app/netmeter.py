"""Contador leve dos pedidos à Binance (para o monitor de capacidade). Só conta; nunca decide nada nem toca em ordens.

`trader.py` e `reader.py` chamam `record` à volta de cada pedido; o corredor chama `drain` uma vez por ciclo.
Um erro HTTP 4xx (ex.: ordem inexistente) é uma resposta normal da exchange: conta como pedido, não como falha.
Falhas = 429/5xx (`errors`) e ligação recusada ou sem resposta (`timeouts`).
"""
import threading

_LOCK = threading.Lock()
_ZERO = {"requests": 0, "errors": 0, "timeouts": 0, "ms_sum": 0.0, "ms_max": 0.0}
_state = dict(_ZERO)


def record(ms, error=False, timeout=False):
    with _LOCK:
        _state["requests"] += 1
        _state["errors"] += 1 if error else 0
        _state["timeouts"] += 1 if timeout else 0
        _state["ms_sum"] += ms
        _state["ms_max"] = max(_state["ms_max"], ms)


def drain():
    """Devolve o que se contou desde a última vez e põe a zero."""
    with _LOCK:
        out = dict(_state)
        _state.update(_ZERO)
    out["ms_avg"] = out["ms_sum"] / out["requests"] if out["requests"] else 0.0
    return out
