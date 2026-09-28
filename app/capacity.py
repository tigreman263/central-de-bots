"""Monitor de capacidade operacional: este equipamento ainda tem margem para o que está a correr?

Funciona igual em Raspberry Pi, PC Windows/Linux, mini-PC, servidor ou VPS: não há regras por modelo nem um número mágico
de bots. Mede o que o sistema realmente sente (atraso do corredor, falhas e latência dos pedidos, fila de operações) e
os recursos da máquina (CPU, RAM, disco), e junta tudo num estado: NORMAL, ATENÇÃO, ELEVADO ou CRÍTICO.
Só informa: nunca pára, pausa nem altera bots. Não toca em ordens, na estratégia nem nas regras financeiras.

Leve por desenho: uma leitura por ciclo do corredor (60 s), sem processos novos, sem pedidos à Binance; o histórico agrega
em intervalos de 5 minutos (uma linha) e guarda 14 dias.
"""
import json
import os
import platform
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import db, netmeter, notify
from .log import log

NORMAL, ATENCAO, ELEVADO, CRITICO = "normal", "atencao", "elevado", "critico"
STATES = [NORMAL, ATENCAO, ELEVADO, CRITICO]
LABELS = {NORMAL: "Normal", ATENCAO: "Atenção", ELEVADO: "Elevado", CRITICO: "Crítico"}
ADVICE = {
    NORMAL: "O equipamento tem margem suficiente. Nenhuma ação necessária.",
    ATENCAO: "A carga do equipamento está a aumentar. Monitorize a capacidade antes de adicionar mais bots.",
    ELEVADO: "Carga elevada. A execução de mais bots poderá afetar o desempenho. Considere migrar para um equipamento "
             "com maior capacidade.",
    CRITICO: "Capacidade operacional comprometida. Evite adicionar novos bots e considere migrar para um equipamento "
             "com maior capacidade.",
}
ALERT_SEVERITY = {ATENCAO: "atenção", ELEVADO: "alto", CRITICO: "alto"}

# Limiares genéricos (valor a partir do qual o indicador passa a ATENÇÃO / ELEVADO / CRÍTICO). NÃO são limites universais:
# a leitura importante é a combinação (ver `evaluate`); um equipamento com a CPU alta e o corredor a horas não está em apuros.
TH = {
    "cpu": (60, 75, 90),                  # % média na janela
    "ram": (88, 94, 97),                  # % em uso (um PC com o sistema e programas abertos vive bem acima de 80 %)
    "disk": (85, 93, 97),                 # % usado
    "runner": (0.3, 0.6, 1.25),           # duração do ciclo / intervalo esperado (>1 = já está atrasado)
    "err_rate": (0.05, 0.10, 0.25),       # (erros + timeouts) / pedidos, com pelo menos 10 pedidos
    "err_count": (5, 12, 30),             # (erros + timeouts) na janela, com poucos pedidos
    "latency": (800, 1500, 3000),         # ms médios por pedido
    "queue": (10, 30, 60),                # operações pendentes (cancelar/enviar/reconciliar)
    "ram_app": (150, 300, 500),           # MB do próprio processo (corredor); muito acima do que central+runner usam normalmente
}
PRIMARY = ("runner", "errors", "queue")   # sintomas de que o SISTEMA já sofre; os outros são recursos da máquina
BANDS = (25, 50, 75)                      # % da barra em que começa cada estado
HISTORY_BUCKET_S = 300
HISTORY_KEEP_DAYS = 14
STALE_AFTER_S = 180


# ---------- recolha do sistema (só biblioteca padrão) ----------
def parse_proc_stat(text):
    """(ocupado, total) a partir da 1.ª linha de /proc/stat."""
    parts = [int(x) for x in text.splitlines()[0].split()[1:9]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)          # idle + iowait
    total = sum(parts)
    return total - idle, total


def parse_meminfo(text):
    """(% em uso, total em bytes) a partir de /proc/meminfo."""
    kb = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if rest.strip():
            kb[name] = int(rest.split()[0])
    total = kb["MemTotal"]
    avail = kb.get("MemAvailable", kb.get("MemFree", 0) + kb.get("Buffers", 0) + kb.get("Cached", 0))
    return (total - avail) / total * 100, total * 1024


def parse_net_dev(text):
    """(bytes recebidos, bytes enviados) de todas as interfaces exceto lo."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if name.strip() != "lo" and len(cols) >= 9:
            rx, tx = rx + int(cols[0]), tx + int(cols[8])
    return rx, tx


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def _win_counters():
    import ctypes

    class FT(ctypes.Structure):
        _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]
    idle, kernel, user = FT(), FT(), FT()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    val = lambda f: (f.hi << 32) | f.lo                              # noqa: E731
    total = val(kernel) + val(user)                                  # o "kernel" já inclui o tempo parado
    return total - val(idle), total


def _own_process_ram_mb():
    """RAM do PRÓPRIO processo (o corredor), não da máquina inteira. Assim a RAM global de outros programas (browser,
    IDE, etc.) nunca é confundida com a da central: `evaluate()` só deixa a RAM do sistema subir o estado sozinha
    quando isto mostra a app também sob pressão (ver `evaluate`).

    Windows: `GetProcessMemoryInfo` (psapi, só biblioteca padrão via ctypes). Linux: `/proc/self/status` (VmRSS).
    Nenhum outro sistema tem leitura própria aqui: `None` é o resultado, e a avaliação cai no comportamento antigo
    (só RAM do sistema) — esse é o fallback documentado, não um erro."""
    try:
        system = platform.system()
        if system == "Windows":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            # sem isto, o handle (pseudo-handle de 64 bits, -1) fica mal convertido e a chamada falha (ERROR_INVALID_HANDLE)
            ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / 1e6
            return None
        if system == "Linux":
            text = _read("/proc/self/status")
            if not text:
                return None
            for line in text.splitlines():
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) * 1024 / 1e6      # kB -> MB
            return None
    except Exception:
        return None
    return None                                                     # macOS e outros: sem leitura própria (fallback)


def _win_memory():
    import ctypes

    class MEM(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32), ("total", ctypes.c_uint64),
                    ("avail", ctypes.c_uint64), ("totalpage", ctypes.c_uint64), ("availpage", ctypes.c_uint64),
                    ("totalvirt", ctypes.c_uint64), ("availvirt", ctypes.c_uint64), ("ext", ctypes.c_uint64)]
    mem = MEM()
    mem.length = ctypes.sizeof(MEM)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        return None
    return float(mem.load), int(mem.total)


class SystemSampler:
    """Uma leitura do estado da máquina. Tudo o que não der para medir neste sistema vem como None (e não conta)."""

    def __init__(self, system=None, data_dir=None, clock=time.monotonic):
        self.system = system or platform.system()
        self.data_dir = str(data_dir or Path.cwd())
        self.clock = clock
        self._cpu = None
        self._net = None

    def _cpu_counters(self):
        if self.system == "Linux":
            text = _read("/proc/stat")
            return parse_proc_stat(text) if text else None
        if self.system == "Windows":
            return _win_counters()
        return None

    def _memory(self):
        if self.system == "Linux":
            text = _read("/proc/meminfo")
            return parse_meminfo(text) if text else None
        if self.system == "Windows":
            return _win_memory()
        return None

    def _network(self):
        text = _read("/proc/net/dev") if self.system == "Linux" else None
        return parse_net_dev(text) if text else None

    def read(self):
        out = {"cpu": None, "ram": None, "ram_total": None, "app_ram_mb": _own_process_ram_mb(), "disk_pct": None,
               "disk_free_gb": None, "net_rx_bps": None, "net_tx_bps": None, "threads": threading.active_count()}
        now = self.clock()
        counters = self._cpu_counters()
        if counters and self._cpu:
            busy, total = counters[0] - self._cpu[0], counters[1] - self._cpu[1]
            out["cpu"] = max(0.0, min(100.0, busy / total * 100)) if total > 0 else None
        elif counters is None and hasattr(os, "getloadavg"):         # macOS e afins: aproximação pela carga média
            out["cpu"] = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
        self._cpu = counters or self._cpu
        mem = self._memory()
        if mem:
            out["ram"], out["ram_total"] = mem
        try:
            usage = shutil.disk_usage(self.data_dir)
            out["disk_pct"], out["disk_free_gb"] = usage.used / usage.total * 100, usage.free / 1e9
        except OSError:
            pass
        net = self._network()
        if net and self._net and now > self._net[2]:
            dt = now - self._net[2]
            out["net_rx_bps"], out["net_tx_bps"] = max(0, net[0] - self._net[0]) / dt, max(0, net[1] - self._net[1]) / dt
        self._net = (net[0], net[1], now) if net else self._net
        return out


def device_info(system=None):
    """O equipamento onde o programa está a correr: só uma descrição, nunca regras por modelo."""
    system = system or platform.system()
    model = None
    if system == "Linux":
        model = (_read("/proc/device-tree/model") or "").strip("\x00\n ") or None
        if not model:
            for line in (_read("/proc/cpuinfo") or "").splitlines():
                if line.lower().startswith("model") and ":" in line and "name" not in line.lower():
                    model = line.split(":", 1)[1].strip()
                    break
    if model and "raspberry" in model.lower():
        kind = "Raspberry Pi"
    elif system == "Windows":
        kind = "PC Windows"
    elif system == "Linux":
        cpuinfo = _read("/proc/cpuinfo") or ""
        kind = "Linux (máquina virtual)" if " hypervisor" in cpuinfo else "PC / servidor Linux"
    elif system == "Darwin":
        kind = "Mac"
    else:
        kind = system or "Equipamento"
    ram_total = None
    mem = SystemSampler(system=system)._memory()
    if mem:
        ram_total = mem[1]
    return {"kind": kind, "model": model, "os": f"{platform.system()} {platform.release()}".strip(),
            "machine": platform.machine(), "cpus": os.cpu_count(), "ram_gb": round(ram_total / 1e9, 1) if ram_total else None,
            "python": platform.python_version()}


# ---------- avaliação (função pura) ----------
def _level(value, th):
    return sum(1 for t in th if value >= t)


def _severity(value, th):
    """0..1 na barra: as fronteiras dos estados caem em 0,25 / 0,50 / 0,75."""
    xs, ys = [0.0, th[0], th[1], th[2], th[2] * 1.15], [0.0, 0.25, 0.5, 0.75, 1.0]
    if value >= xs[-1]:
        return 1.0
    for i in range(1, len(xs)):
        if value <= xs[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (value - xs[i - 1]) / (xs[i] - xs[i - 1])
    return 1.0


def _band_top(level):
    return 1.0 if level >= 3 else 0.25 * (level + 1) - 0.001


def _state_of(pct):
    return STATES[sum(1 for b in BANDS if pct >= b)]


def _median(values):
    values = sorted(values)
    return None if not values else (values[len(values) // 2] if len(values) % 2 else (values[len(values) // 2 - 1] + values[len(values) // 2]) / 2)


def aggregate(samples, recent=3):
    """Junta as últimas leituras (janela) nos números que a avaliação usa. Sem leituras: tudo None.

    A duração do ciclo usa a mediana dos últimos `recent` ciclos: um pico isolado não conta, dois lentos seguidos sim."""
    def avg(key):
        vals = [s[key] for s in samples if s.get(key) is not None]
        return sum(vals) / len(vals) if vals else None
    last = samples[-1] if samples else {}
    tail = list(samples)[-recent:]
    requests = sum(s.get("requests", 0) for s in samples)
    ms_sum = sum(s.get("req_ms_avg", 0.0) * s.get("requests", 0) for s in samples)
    return {"cpu": avg("cpu"), "cpu_max": max([s["cpu"] for s in samples if s.get("cpu") is not None], default=None),
            "ram": last.get("ram"), "ram_app_mb": last.get("app_ram_mb"),
            "disk_pct": last.get("disk_pct"), "disk_free_gb": last.get("disk_free_gb"),
            "interval": last.get("interval"), "dur_last": last.get("dur"),
            "dur_recent": _median([s["dur"] for s in tail if s.get("dur") is not None]),
            "requests": requests, "failures": sum(s.get("errors", 0) + s.get("timeouts", 0) for s in samples),
            "timeouts": sum(s.get("timeouts", 0) for s in samples),
            "latency": ms_sum / requests if requests else None,
            "pending": last.get("pending"), "bots_active": last.get("bots_active"),
            "net_rx_bps": last.get("net_rx_bps"), "net_tx_bps": last.get("net_tx_bps")}


def evaluate(m):
    """Estado composto a partir das métricas agregadas `m` (ver `aggregate`).

    Sintomas do sistema (atraso do corredor, erros/timeouts, fila) mandam. Os recursos (CPU, latência) sozinhos nunca
    passam de ATENÇÃO enquanto o sistema não sofre; disco só o ultrapassa quando está perto de esgotar (limite real).

    RAM é o caso especial: a leitura do sistema operativo é de TODO o equipamento (Windows, browser, outros
    programas incluídos), nunca só da app. Por isso a RAM do sistema só sobe o estado sozinha quando a RAM do
    PRÓPRIO processo (`ram_app`) confirma que é a app a sofrer, ou quando já há outro sintoma do sistema. Sem essa
    confirmação (ou sem conseguir medir o processo neste sistema operativo — fallback) fica só informativa, presa a
    NORMAL: uma central saudável não fica em ATENÇÃO só porque o browser ou o Claude Code enchem a máquina.
    Devolve o estado, a % da barra, os indicadores e o conselho.
    """
    inds = {}

    def add(key, label, value, th, text, primary=False, boost=None):
        if value is None:
            return
        lvl, sev = _level(value, th), _severity(value, th)
        if boost:
            lvl, sev = max(lvl, boost[0]), max(sev, boost[1])
        inds[key] = {"key": key, "label": label, "text": text, "raw": lvl, "sev": sev, "primary": primary}

    interval = m.get("interval") or 0
    dur = m.get("dur_recent")
    if dur is not None and interval:
        lag = max(0.0, dur - interval)
        add("runner", "Runner", dur / interval, TH["runner"], f"+{lag:.1f} s" if lag > 0 else
            ("Normal" if dur / interval < TH["runner"][0] else f"Ocupado ({dur:.0f} s de {interval:.0f} s)"),
            primary=True, boost=(2, 0.5) if lag > 0 else None)
    failures, requests = m.get("failures", 0), m.get("requests", 0)
    if requests:
        lvl_c, sev_c = _level(failures, TH["err_count"]), _severity(failures, TH["err_count"])
        if requests >= 10:
            lvl_c = max(lvl_c, _level(failures / requests, TH["err_rate"]))
            sev_c = max(sev_c, _severity(failures / requests, TH["err_rate"]))
        inds["errors"] = {"key": "errors", "label": "Erros", "raw": lvl_c, "sev": sev_c, "primary": True,
                          "text": "0" if not failures else f"{failures} ({m.get('timeouts', 0)} timeouts)"}
    if m.get("pending") is not None:
        add("queue", "Fila", m["pending"], TH["queue"], "Normal" if m["pending"] < TH["queue"][0] else f"{m['pending']} pendentes",
            primary=True)
    add("latency", "Latência", m.get("latency"), TH["latency"], f"{(m.get('latency') or 0):.0f} ms")
    add("cpu", "CPU", m.get("cpu"), TH["cpu"], f"{(m.get('cpu') or 0):.0f} %")
    add("ram", "RAM (sistema)", m.get("ram"), TH["ram"], f"{(m.get('ram') or 0):.0f} %")
    app_mb = m.get("ram_app_mb")
    if app_mb is not None:
        add("ram_app", "RAM (central)", app_mb, TH["ram_app"], f"{app_mb:.0f} MB")
    add("disk", "Disco", m.get("disk_pct"), TH["disk"], f"{(m.get('disk_pct') or 0):.0f} %")

    pmax = max([i["raw"] for i in inds.values() if i["primary"]], default=0)
    app_ind = inds.get("ram_app")
    app_confirmed_healthy = app_ind is not None and app_ind["raw"] == 0   # medimos o processo E está bem: RAM do sistema não conta só
    caps = {"cpu": 1 + pmax, "latency": 1 + pmax, "ram": 0 if (pmax == 0 and app_confirmed_healthy) else 1 + pmax,
            "ram_app": 1 + pmax, "disk": 1 + pmax}
    if (m.get("ram") or 0) >= TH["ram"][1] and not app_confirmed_healthy:
        caps["ram"] = 3                                              # memória quase esgotada E a app não está confirmada saudável
    if (app_mb or 0) >= TH["ram_app"][1]:
        caps["ram_app"] = 3                                          # a app em si está a usar muita RAM: isto é real, sem atenuar
    if (m.get("disk_pct") or 0) >= TH["disk"][1]:
        caps["disk"] = 3
    if m.get("disk_free_gb") is not None and m["disk_free_gb"] < 0.5:
        caps["disk"] = 3
        inds["disk"] = {**inds.get("disk", {"key": "disk", "label": "Disco", "text": f"{m['disk_free_gb']:.1f} GB livres",
                                            "primary": False}), "raw": 3, "sev": 1.0}
    for key, ind in inds.items():
        cap = caps.get(key, 3)
        ind["level"], ind["sev"] = min(ind["raw"], cap), min(ind["sev"], _band_top(min(ind["raw"], cap)))
    worst = max((i["sev"] for i in inds.values()), default=0.0)
    if pmax >= 1:                                                    # com um sintoma do sistema, vários sinais agravam
        band = sum(1 for b in BANDS if worst * 100 >= b)
        others = sum(1 for i in inds.values() if i["level"] >= 1 and i["sev"] < worst)
        worst = min(_band_top(band), worst + min(0.10, 0.03 * others))                 # dentro da mesma faixa...
        if band in (1, 2) and sum(1 for i in inds.values() if i["level"] >= band) >= 3:
            worst = max(worst, 0.25 * (band + 1))                                        # ...e 3+ na mesma faixa sobem de estado
    pct = int(worst * 100 + 1e-9)
    state = _state_of(pct)
    net_parts = [inds[k] for k in ("errors", "latency") if k in inds]
    net = max(net_parts, key=lambda i: i["level"], default=None)
    display = [{"label": "CPU", "text": inds["cpu"]["text"] if "cpu" in inds else "n/d", "level": inds.get("cpu", {}).get("level", 0)},
               {"label": "RAM", "text": _ram_text(inds), "level": max(inds.get("ram", {}).get("level", 0), inds.get("ram_app", {}).get("level", 0))},
               {"label": "Disco", "text": inds["disk"]["text"] if "disk" in inds else "n/d", "level": inds.get("disk", {}).get("level", 0)},
               {"label": "Runner", "text": inds["runner"]["text"] if "runner" in inds else "sem ciclos",
                "level": inds.get("runner", {}).get("level", 0)},
               {"label": "Rede", "text": _net_text(net, inds) if net else "sem pedidos", "level": net["level"] if net else 0},
               {"label": "Fila", "text": inds["queue"]["text"] if "queue" in inds else "n/d", "level": inds.get("queue", {}).get("level", 0)}]
    return {"state": state, "level": STATES.index(state), "pct": pct, "indicators": list(inds.values()), "display": display,
            "advice": ADVICE[state]}


def _ram_text(inds):
    """Uma só linha para a tabela existente (sem nova linha na interface): sistema + central, quando ambos existem."""
    sys_text = inds["ram"]["text"] if "ram" in inds else "n/d"
    if "ram_app" in inds:
        return f"{sys_text} · central {inds['ram_app']['text']}"
    return sys_text


def _net_text(net, inds):
    if net["level"] == 0:
        return "Normal"
    if net["key"] == "errors":
        return f"Com falhas ({inds['errors']['text']})"
    return f"Lenta ({net['text']})"


# ---------- monitor (estado entre ciclos) ----------
class Monitor:
    """Recebe uma leitura por ciclo, avalia a janela recente e só muda de estado com confirmação (sem oscilar).

    Subir exige `up` avaliações seguidas acima do estado atual; descer exige `down` seguidas abaixo (e desce para o pior
    valor dessas leituras, por degraus). Assim um pico isolado não gera alerta e a recuperação também é confirmada."""

    def __init__(self, sampler=None, window=10, up=2, down=5, data_dir=None):
        self.sampler = sampler or SystemSampler(data_dir=data_dir)
        self.samples = deque(maxlen=window)
        self.up, self.down = up, down
        self.state = NORMAL
        self._raise, self._lower = 0, []
        self.device = None
        self.bucket = None
        self.ready = False

    def restore(self, state):
        if state in STATES:
            self.state = state

    def measure(self, t0, t1, interval_s, result=None, bots_active=0, pending=0):
        """Junta a leitura do sistema com o que o corredor mediu do próprio ciclo e o que os pedidos à Binance mostraram."""
        net = netmeter.drain()
        sample = dict(self.sampler.read())
        dur = max(0.0, t1 - t0)
        sample.update(ts=t1, dur=dur, interval=interval_s, lag=max(0.0, dur - interval_s),
                      bots_processed=(result or {}).get("bots", 0), cycle_errors=(result or {}).get("errors", 0),
                      pending=pending, bots_active=bots_active, requests=net["requests"], errors=net["errors"],
                      timeouts=net["timeouts"], req_ms_avg=net["ms_avg"], req_ms_max=net["ms_max"])
        return sample

    def observe(self, sample):
        self.samples.append(sample)
        assessment = evaluate(aggregate(list(self.samples)))
        raw = assessment["state"]
        old, new = self.state, self.state
        if STATES.index(raw) > STATES.index(old):
            self._raise += 1
            self._lower = []
            if self._raise >= self.up:
                new, self._raise = raw, 0
        elif STATES.index(raw) < STATES.index(old):
            self._lower.append(raw)
            self._raise = 0
            if len(self._lower) >= self.down:
                new, self._lower = max(self._lower, key=STATES.index), []
        else:
            self._raise, self._lower = 0, []
        self.state = new
        idx = STATES.index(new)                                          # a barra fica dentro da faixa do estado mostrado
        lo, hi = (0 if idx == 0 else BANDS[idx - 1]), (BANDS[idx] - 1 if idx < 3 else 100)
        assessment.update(raw_state=raw, state=new, level=STATES.index(new), advice=ADVICE[new],
                          pct=max(lo, min(hi, assessment["pct"])))
        return assessment, ((old, new) if new != old else None)


# ---------- gravação (leve) ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS capacity_history (
    bucket INTEGER PRIMARY KEY, samples INTEGER NOT NULL, cpu_sum REAL NOT NULL, cpu_n INTEGER NOT NULL, cpu_max REAL,
    ram_sum REAL NOT NULL, ram_n INTEGER NOT NULL, ram_max REAL, cycles INTEGER NOT NULL, dur_sum REAL NOT NULL,
    dur_max REAL NOT NULL, lag_max REAL NOT NULL, cycle_errors INTEGER NOT NULL, requests INTEGER NOT NULL,
    errors INTEGER NOT NULL, timeouts INTEGER NOT NULL, ms_sum REAL NOT NULL, ms_max REAL NOT NULL,
    pending_max INTEGER NOT NULL, bots_max INTEGER NOT NULL, pct_max INTEGER NOT NULL, level_max INTEGER NOT NULL
);
"""


def init(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _new_bucket(ts):
    return {"bucket": int(ts // HISTORY_BUCKET_S * HISTORY_BUCKET_S), "samples": 0, "cpu_sum": 0.0, "cpu_n": 0, "cpu_max": None,
            "ram_sum": 0.0, "ram_n": 0, "ram_max": None, "cycles": 0, "dur_sum": 0.0, "dur_max": 0.0, "lag_max": 0.0,
            "cycle_errors": 0, "requests": 0, "errors": 0, "timeouts": 0, "ms_sum": 0.0, "ms_max": 0.0, "pending_max": 0,
            "bots_max": 0, "pct_max": 0, "level_max": 0}


def _add_to_bucket(b, s, assessment):
    b["samples"] += 1
    for key in ("cpu", "ram"):
        if s.get(key) is not None:
            b[f"{key}_sum"] += s[key]
            b[f"{key}_n"] += 1
            b[f"{key}_max"] = max(b[f"{key}_max"] or 0.0, s[key])
    b["cycles"] += 1
    b["dur_sum"] += s["dur"]
    b["dur_max"] = max(b["dur_max"], s["dur"])
    b["lag_max"] = max(b["lag_max"], s["lag"])
    b["cycle_errors"] += s["cycle_errors"]
    b["requests"] += s["requests"]
    b["errors"] += s["errors"]
    b["timeouts"] += s["timeouts"]
    b["ms_sum"] += s["req_ms_avg"] * s["requests"]
    b["ms_max"] = max(b["ms_max"], s["req_ms_max"])
    b["pending_max"] = max(b["pending_max"], s["pending"])
    b["bots_max"] = max(b["bots_max"], s["bots_active"])
    b["pct_max"] = max(b["pct_max"], assessment["pct"])
    b["level_max"] = max(b["level_max"], assessment["level"])


def _flush_bucket(conn, b, now):
    cols = list(b)
    conn.execute(f"INSERT OR REPLACE INTO capacity_history ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 [b[c] for c in cols])
    conn.execute("DELETE FROM capacity_history WHERE bucket < ?", (int(now - HISTORY_KEEP_DAYS * 86400),))
    conn.commit()


def pending_ops(conn):
    """Trabalho à espera de ir para a exchange: ordens por enviar ou reconciliar e cancelamentos na fila (todos os bots)."""
    n = conn.execute("SELECT COUNT(*) FROM bot_orders WHERE state IN ('sending', 'settling')").fetchone()[0]
    for row in conn.execute("SELECT state FROM bots WHERE mode = 'testnet'").fetchall():
        try:
            n += len(json.loads(row[0] or "{}").get("cancel_queue") or [])
        except ValueError:
            pass
    return n


def _alert(conn, old, new, assessment, now):
    """Só quando o estado muda: aviso ao subir, informação ao recuperar. Usa o sistema de alertas que já existe."""
    key = f"capacity:{int(now)}:{new}"
    if STATES.index(new) > STATES.index(old):
        msg = f"Capacidade do equipamento: {LABELS[new].upper()}. {ADVICE[new]}"
        severity = ALERT_SEVERITY[new]
    elif new == NORMAL:
        msg, severity = "Capacidade do equipamento: voltou a NORMAL. A carga já tem margem.", "info"
    else:
        msg, severity = f"Capacidade do equipamento: desceu para {LABELS[new].upper()} (estava {LABELS[old].upper()}).", "info"
    notify.system_alert(conn, key, severity, msg, coin="Sistema", criterion="capacidade")


def after_cycle(conn, mon, t0, t1, interval_s, result=None, now=None):
    """O corredor chama isto no fim de cada ciclo. NUNCA levanta erro: a monitorização não pode afetar os bots."""
    try:
        now = now if now is not None else time.time()
        if not mon.ready:
            init(conn)
            mon.device = device_info()
            snap = load_snapshot(conn)
            if snap:
                mon.restore(snap.get("state"))                       # depois de um reinício não repete o aviso
            mon.ready = True
        bots = conn.execute("SELECT COUNT(*) FROM bots WHERE status != 'stopped'").fetchone()[0]
        sample = mon.measure(t0, t1, interval_s, result, bots_active=bots, pending=pending_ops(conn))
        assessment, change = mon.observe(sample)
        b = mon.bucket
        if b is not None and b["bucket"] != int(now // HISTORY_BUCKET_S * HISTORY_BUCKET_S):
            _flush_bucket(conn, b, now)                              # fecha o intervalo de 5 min: uma linha
            b = mon.bucket = None
        if b is None:
            b = mon.bucket = _new_bucket(now)
        _add_to_bucket(b, sample, assessment)
        snapshot = {"ts": now, "state": assessment["state"], "raw_state": assessment["raw_state"], "pct": assessment["pct"],
                    "advice": assessment["advice"], "display": assessment["display"], "indicators": assessment["indicators"],
                    "device": mon.device, "bots_active": bots, "pending": sample["pending"],
                    "cycle": {"dur": sample["dur"], "interval": interval_s, "lag": sample["lag"],
                              "bots": sample["bots_processed"], "errors": sample["cycle_errors"]},
                    "system": {k: sample.get(k) for k in ("cpu", "ram", "disk_pct", "disk_free_gb", "net_rx_bps", "net_tx_bps",
                                                          "threads")}}
        db.set_many(conn, {"capacity_snapshot": json.dumps(snapshot)})
        if change:
            _alert(conn, change[0], change[1], assessment, now)
        return assessment
    except Exception:
        log.exception("monitor de capacidade falhou (não afeta os bots)")
        return None


# ---------- leitura para o painel ----------
def load_snapshot(conn):
    try:
        raw = db.get(conn, "capacity_snapshot")
        return json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return None


def history_summary(conn, hours=24, now=None):
    """Médias e máximos do período (a partir das linhas agregadas). None se ainda não há histórico."""
    now = now if now is not None else time.time()
    try:
        row = conn.execute(
            "SELECT SUM(samples) n, SUM(cpu_sum) cs, SUM(cpu_n) cn, MAX(cpu_max) cmax, SUM(ram_sum) rs, SUM(ram_n) rn, "
            "MAX(ram_max) rmax, SUM(cycles) cy, SUM(dur_sum) ds, MAX(dur_max) dmax, MAX(lag_max) lmax, SUM(requests) rq, "
            "SUM(errors) er, SUM(timeouts) tm, SUM(ms_sum) ms, MAX(ms_max) mmax, MAX(pending_max) pmax, MAX(bots_max) bmax, "
            "MAX(pct_max) pct FROM capacity_history WHERE bucket >= ?", (int(now - hours * 3600),)).fetchone()
    except Exception:
        return None
    if not row or not row["n"]:
        return None
    return {"hours": hours, "cpu_avg": row["cs"] / row["cn"] if row["cn"] else None, "cpu_max": row["cmax"],
            "ram_avg": row["rs"] / row["rn"] if row["rn"] else None, "ram_max": row["rmax"],
            "cycle_avg": row["ds"] / row["cy"] if row["cy"] else None, "cycle_max": row["dmax"], "lag_max": row["lmax"],
            "requests": row["rq"], "errors": row["er"], "timeouts": row["tm"],
            "latency_avg": row["ms"] / row["rq"] if row["rq"] else None, "latency_max": row["mmax"],
            "pending_max": row["pmax"], "bots_max": row["bmax"], "pct_max": row["pct"]}


PILL = {NORMAL: "gain", ATENCAO: "warn", ELEVADO: "warn", CRITICO: "loss"}
BAR = {NORMAL: "var(--gain)", ATENCAO: "#C99A00", ELEVADO: "#D9730D", CRITICO: "var(--loss)"}


def view(conn, now=None):
    """Tudo o que o painel precisa, já pronto para mostrar (ou None se o corredor ainda nunca mediu)."""
    now = now if now is not None else time.time()
    snap = load_snapshot(conn)
    if not snap:
        return {"available": False, "label": "Sem medições", "pill": "idle", "advice":
                "O corredor ainda não mediu a capacidade (começa no primeiro ciclo)."}
    age = now - snap["ts"]
    state = snap["state"]
    out = {"available": True, "fresh": age < STALE_AFTER_S, "age_s": int(age), "state": state, "label": LABELS[state],
           "pill": PILL[state], "bar": BAR[state], "pct": snap["pct"], "advice": snap["advice"], "display": snap["display"],
           "device": snap.get("device") or {}, "bots_active": snap["bots_active"], "cycle": snap["cycle"],
           "system": snap["system"], "pending": snap["pending"], "history": history_summary(conn, now=now)}
    if age >= STALE_AFTER_S:                                         # o corredor parou: não mostrar um "Normal" antigo
        out.update(pill="idle", label="Sem dados recentes", advice="O corredor não mediu nos últimos minutos.")
    return out
