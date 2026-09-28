"""Malha, v0.1: painel com palavra-passe, configuração, preços e PARAR TUDO.

Só modo simulação. Nada aqui envia ordens.
"""
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import alerts, analysis, balance, botstore, capacity, chart, config as cfgmod, explain, costbasis, db, keystore, layout, market, notify, pairs, portfolio, readiness, reserve, risk, staking, validation
from . import grid as G
from . import recommendations as rec
from .log import log
from .reader import BinanceError, BinanceReader, check_permissions, check_trading_permissions
from .trader import Trader, TraderError
from .risk import RANK
from .rules import EXCLUSION_CATEGORIES, RISK_PAIRS, describe_changes, parse_config

CURRENCIES = ("USDT", "EUR", "USD")
PUBLIC_ENDPOINTS = {"login", "setup", "static"}
MAX_FAILS, FAIL_WINDOW = 5, 300
# estado do bot: texto com símbolo (a cor nunca é o único sinal) e classe da pílula; "parado" não é erro
STATES = {"running": ("● A trabalhar", "gain"), "paused": ("⏸ Em pausa", "warn"),
          "pending": ("◌ A arrancar", "warn"), "stopped": ("■ Parado", "idle"),
          "recovering": ("⟳ A recuperar", "warn"), "stopping": ("⏳ A parar", "warn")}
LOCAL_ADDRS = ("127.0.0.1", "::1")
MAX_GLOBAL_FAILS = 30
MAX_BOT_CAPITAL = 1_000_000   # USDT; só um travão de bom senso contra um erro de dactilografia, não um limite de negócio
PLACEHOLDERS = {}


def create_app(test_config=None):
    app = Flask(__name__)
    root = Path(__file__).resolve().parent.parent
    app.config.update(DATA_DIR=str(cfgmod.data_dir()), MARKET_FETCH=None, KEY_FILE=None, READER_FACTORY=None,
                      TESTNET_KEY_FILE=None, TELEGRAM_FILE=None, WHATSAPP_FILE=None, TRADER_FACTORY=None,
                      REAL_TRADING_KEY_FILE=None)
    if test_config:
        app.config.update(test_config)

    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config["DB_PATH"] = str(data_dir / "app.db")
    db.init_db(app.config["DB_PATH"])
    _c = db.connect(app.config["DB_PATH"])
    alerts.init(_c)
    botstore.init(_c)
    _c.close()

    key_file = data_dir / "secret_key"
    if not key_file.exists():
        keystore.write_private(key_file, secrets.token_hex(32))       # nasce já com 0600: ninguém mais a lê
    app.secret_key = key_file.read_text().strip()
    if os.name != "nt":                                              # dados só para o utilizador do serviço
        for target, mode in ((data_dir, 0o700), (Path(app.config["DB_PATH"]), 0o600)):
            try:
                os.chmod(target, mode)
            except OSError:
                pass
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=os.environ.get("BOTS_HTTPS") == "1",   # ligar atrás de HTTPS/VPN
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    app.extensions["fails"] = {}
    app.extensions["gfails"] = []

    # ---------- ligação à base de dados por pedido ----------
    def conn():
        if "db" not in g:
            g.db = db.connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    def has_password():
        return db.get(conn(), "password_hash") is not None

    # ---------- segurança: nada se vê sem login; CSRF em todos os POST ----------
    @app.before_request
    def guard():
        if request.endpoint in PUBLIC_ENDPOINTS:
            pass
        elif not has_password():
            return redirect(url_for("setup"))
        elif not session.get("auth"):
            return redirect(url_for("login"))
        if request.method == "POST":
            token = session.get("csrf")
            if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
                abort(400, "Pedido inválido. Volta atrás e tenta de novo.")

    @app.before_request
    def make_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.after_request
    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{g.get('csp_nonce', '')}'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        return resp

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        stopped = False
        stopping_n = 0
        dot = None
        if session.get("auth"):
            stopped = db.get(conn(), "emergency_stop") == "1"
            if stopped:                                   # o estado real: ainda há bots por confirmar como parados?
                stopping_n = sum(1 for r in botstore.all_bots(conn())
                                 if r["status"] != "stopped" or botstore.has_pending(conn(), r))
            dot = {"alto": "alto", "atenção": "atencao", "info": "info"}.get(alerts.unseen_top(conn()))
        return {
            "csp_nonce": g.get("csp_nonce", ""),
            "states": STATES,
            "alert_dot": dot,
            "gl": explain, "rec": {"general": rec.GENERAL, "fields": rec.FIELDS, "disclaimer": rec.DISCLAIMER, "risk_items": rec.RISK_ITEMS},
            "csrf": session["csrf"],
            "cur": session.get("cur", "USDT"),
            "currencies": CURRENCIES,
            "stopped": stopped, "stopping_n": stopping_n,
            "logged_in": bool(session.get("auth")),
            "endpoint": request.endpoint,
        }

    # ---------- dinheiro e conversões ----------
    def to_currency(eur, mkt, cur):
        """Converte EUR para a moeda escolhida; None se não houver preços reais."""
        if cur == "EUR":
            return eur
        if not mkt["ok"]:
            return None
        return eur / mkt["eur_per_usdt"]  # USDT; USD trata-se como 1:1 com USDT

    @app.template_filter("money")
    def money(value, cur="EUR"):
        if value is None:
            return "sem preços"
        text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
        return f"{text} {cur}"

    @app.template_filter("datefmt")
    def datefmt(seconds):
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    @app.template_filter("signed")
    def signed(value):
        text = f"{abs(value):,.2f}".replace(",", " ").replace(".", ",")
        return ("+" if value >= 0 else "−") + text

    @app.template_filter("price")
    def price(value):
        if value is None:
            return "sem preço"
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")

    def get_market():
        return market.get_market(app.config["MARKET_FETCH"] or market._download)

    # ---------- entrada ----------
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if has_password():
            return redirect(url_for("login"))
        if request.remote_addr not in LOCAL_ADDRS and os.environ.get("BOTS_SETUP_ALLOW_LAN") != "1":
            abort(403, "A palavra-passe cria-se no próprio computador (127.0.0.1), ou por um túnel SSH. "
                       "Quem chegasse primeiro pela rede ficava com o controlo.")
        if request.method == "POST":
            p1, p2 = request.form.get("password", ""), request.form.get("password2", "")
            if len(p1) < 12:
                flash("A palavra-passe precisa de pelo menos 12 caracteres.", "error")
            elif p1 != p2:
                flash("As duas palavras-passe não são iguais.", "error")
            else:
                db.set_many(conn(), {"password_hash": generate_password_hash(p1)})
                db.log(conn(), "Palavra-passe criada")
                session.clear()
                session["auth"] = True
                session.permanent = True
                return redirect(url_for("inicio"))
        return render_template("setup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not has_password():
            return redirect(url_for("setup"))
        now = time.time()
        by_ip = app.extensions["fails"]
        for ip in [k for k, v in by_ip.items() if not any(now - t < FAIL_WINDOW for t in v)]:
            del by_ip[ip]                                             # o registo de IPs não cresce sem limite
        fails = by_ip.setdefault(request.remote_addr, [])
        fails[:] = [t for t in fails if now - t < FAIL_WINDOW]
        gfails = app.extensions["gfails"]
        gfails[:] = [t for t in gfails if now - t < FAIL_WINDOW]     # atrás de um proxy/VPN todos partilham o IP
        if request.method == "POST":
            if len(fails) >= MAX_FAILS or len(gfails) >= MAX_GLOBAL_FAILS:
                flash("Demasiadas tentativas. Espera 5 minutos e tenta de novo.", "error")
            elif check_password_hash(db.get(conn(), "password_hash"), request.form.get("password", "")):
                fails.clear()
                session.clear()
                session["auth"] = True
                session.permanent = True
                return redirect(url_for("inicio"))
            else:
                fails.append(now)
                gfails.append(now)
                flash("Palavra-passe errada.", "error")
        return render_template("login.html")

    @app.post("/sair")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/moeda/<code>")
    def set_currency(code):
        # só aceita o pedido vindo do próprio painel (outro site não consegue trocar a moeda)
        same_site = request.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")
        ref = urlparse(request.referrer or "")
        if code in CURRENCIES and same_site and (not ref.netloc or ref.netloc == request.host):
            session["cur"] = code
        back = request.referrer if request.referrer and urlparse(request.referrer).netloc == request.host else None
        return redirect(back or url_for("inicio"))

    # ---------- ecrãs ----------
    @app.get("/")
    def inicio():
        cfg = db.get_all(conn())
        mkt = get_market()
        cur = session.get("cur", "USDT")
        capital = float(cfg["capital_eur"])
        capital_label = "Capital simulado"
        snap = load_snapshot()
        if snap and snap.get("eur_per_usdt"):
            capital = snap["total_usdt"] * snap["eur_per_usdt"]  # valor real lido da Binance
            capital_label = "Portefólio real (lido às " + snap["ts"][11:16] + " UTC)"
        buckets = []
        for label, key, color in [("Reserva", "split_reserve", "accent"),
                                  ("Trading", "split_trading", "gain"),
                                  ("Caixa", "split_cash", "muted")]:
            pct = float(cfg[key])
            buckets.append({"label": label, "pct": pct, "color": color,
                            "value": to_currency(capital * pct / 100, mkt, cur)})
        prices = []
        for name, sym in [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")]:
            usdt = mkt["prices"].get(sym)
            prices.append({
                "name": name,
                "usdt": usdt,
                "eur": usdt * mkt["eur_per_usdt"] if usdt else None,
            })
        return render_template("inicio.html", total=to_currency(capital, mkt, cur),
                               buckets=buckets, mkt=mkt, prices=prices, cfg=cfg,
                               capital_label=capital_label, cap=capacity.view(conn()))

    CFG_CATS = [("capital", "Capital e saldo"), ("risco", "Risco e lucro"), ("portefolio", "Alertas de risco da carteira"),
                ("sugestoes", "Sugestões de moedas"), ("alertas", "Notificações"), ("ligacoes", "Ligações"),
                ("sistema", "Sistema")]
    FORM_CATS = ["capital", "risco", "portefolio", "sugestoes"]
    ALERT_EXAMPLES = [
        ("Bot em pausa (queda forte, limite diário ou capital em queda)", "atenção", "Ver o motivo no bot. Ele já cancelou as compras."),
        ("Compras suspensas (várias compras seguidas sem venda)", "atenção", "Sinal de queda contínua: esperar ou parar."),
        ("Bot parado (stop-loss, pedido teu ou PARAR TUDO)", "alto", "Confirmar o que ficou por fechar."),
        ("Reset da Testnet", "alto", "Criar um bot novo (a janela de 7 dias reinicia)."),
        ("Ordem recusada pela guarda ou pela exchange", "alto", "Ver o motivo; o bot ficou em pausa."),
        ("Erro interno ou chaves recusadas", "alto", "Ver os registos e as chaves em Ligações."),
        ("O corredor deixou de dar sinal", "alto", "Reiniciar o corredor (no Pi: systemctl restart bots-corredor)."),
    ]

    def alert_context():
        tg = keystore.load(telegram_file())
        wa = keystore.load_json(whatsapp_file())
        return {"prefs": notify.prefs(conn()), "telegram": bool(tg), "telegram_masked": keystore.masked(tg[0]) if tg else "",
                "whatsapp": bool(wa), "whatsapp_cfg": wa or {}, "providers": notify.PROVIDERS,
                "min_choices": notify.MIN_CHOICES, "examples": ALERT_EXAMPLES}

    def system_context():
        rows = botstore.all_bots(conn())
        try:
            db_mb = os.path.getsize(app.config["DB_PATH"]) / 1_048_576
        except OSError:
            db_mb = 0.0
        return {"real_key": bool(keystore.load(key_file())), "testnet_key": bool(keystore.load(testnet_file())),
                "runner": runner_state(), "emergency": db.get(conn(), "emergency_stop") == "1", "bots": len(rows),
                "bots_active": sum(1 for r in rows if r["status"] != "stopped"), "data_dir": str(data_dir),
                "keys_dir": str(cfgmod.keys_dir()), "db_mb": db_mb, "capacity": capacity.view(conn()),
                "gate": readiness.evaluate(conn(), now_ms())}

    def config_page(cfg, cat):
        cat = cat if cat in dict(CFG_CATS) else "capital"
        last = app.extensions.get("pair_cache") or {}
        pair_status = {"count": len(last["data"]), "level": last.get("level", 0), "note": last.get("note", "")} \
            if last.get("data") is not None and not last.get("error") else None
        return render_template("config.html", cfg=cfg, categories=EXCLUSION_CATEGORIES, risk_pairs=RISK_PAIRS, cat=cat,
                               cfg_cats=CFG_CATS, form_cats=FORM_CATS, al=alert_context(), sysinfo=system_context(),
                               pair_status=pair_status, pair_criteria=pairs.describe(pairs.params_from(cfg)))

    @app.route("/configuracao", methods=["GET", "POST"])
    def config():
        cfg = db.get_all(conn())
        if request.method == "POST":
            cat = request.form.get("cat", "capital")
            new, errors = parse_config(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
                return config_page(_merge(cfg, request.form), cat)
            if request.form.get("step") == "save":
                db.set_many(conn(), new)
                app.extensions.pop("pair_cache", None)                  # os critérios mudaram: as sugestões refazem-se
                changes = describe_changes(cfg, new)
                db.log(conn(), "Configuração guardada",
                       "; ".join(f"{a}: {b} → {c}" for a, b, c in changes))
                flash("Configuração guardada.", "ok")
                return redirect(url_for("config", cat=cat))
            changes = describe_changes(cfg, new)
            if not changes:
                flash("Não mudaste nada.", "ok")
                return redirect(url_for("config", cat=cat))
            return render_template("config_confirm.html", changes=changes, new=new, cat=cat)
        return config_page(cfg, request.args.get("cat", "capital"))

    @app.post("/configuracao/alertas")
    def alert_settings():
        action = request.form.get("action", "")
        text = "Malha: mensagem de teste. Se a lês, os alertas estão a funcionar."
        if action == "save_prefs":
            values = {}
            for ch in ("telegram", "whatsapp"):
                values[f"alert_{ch}_on"] = "1" if request.form.get(f"alert_{ch}_on") == "1" else "0"
                minimum = request.form.get(f"alert_{ch}_min", "")
                values[f"alert_{ch}_min"] = minimum if minimum in ("atenção", "alto") else notify.DEFAULT_PREFS[f"alert_{ch}_min"]
            db.set_many(conn(), values)
            db.log(conn(), "Preferências de alertas guardadas", ", ".join(f"{k}={v}" for k, v in values.items()))
            flash("Preferências de alertas guardadas.", "ok")
        elif action == "delete_telegram":
            keystore.delete(telegram_file())
            db.log(conn(), "Telegram removido")
            flash("Telegram removido.", "ok")
        elif action in ("save_telegram", "test_telegram"):
            creds = keystore.load(telegram_file())
            if action == "save_telegram":
                token, chat = request.form.get("token", "").strip(), request.form.get("chat_id", "").strip()
            else:
                token, chat = creds if creds else ("", "")
            if not token or not chat:
                flash("Preenche o token do bot e o id da conversa.", "error")
            else:
                ok, err = (notify.SENDER or notify.send)(token, chat, text)
                if not ok:
                    flash(f"Não consegui enviar: {err}. Confirma o token e o id da conversa.", "error")
                else:
                    if action == "save_telegram":
                        keystore.save(telegram_file(), token, chat)
                        db.log(conn(), "Telegram configurado (só envio)")
                    flash("Mensagem de teste enviada. O Telegram só envia alertas, não recebe comandos.", "ok")
        elif action == "delete_whatsapp":
            keystore.delete(whatsapp_file())
            db.log(conn(), "WhatsApp removido")
            flash("WhatsApp removido.", "ok")
        elif action in ("save_whatsapp", "save_whatsapp_notest", "test_whatsapp"):
            if action == "test_whatsapp":
                wa = keystore.load_json(whatsapp_file())
                errors = [] if wa else ["Ainda não há WhatsApp configurado."]
            else:
                wa, errors = notify.validate_whatsapp(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                ok, err = (True, "") if action == "save_whatsapp_notest" else (notify.WA_SENDER or notify.whatsapp_send)(wa, text)
                if not ok:
                    flash(f"Não consegui enviar: {err}.", "error")
                else:
                    if action != "test_whatsapp":
                        keystore.save_json(whatsapp_file(), wa)
                        db.log(conn(), "WhatsApp configurado (só envio)", notify.PROVIDERS[wa["provider"]])
                    flash("Mensagem de teste enviada. O WhatsApp só envia alertas, não recebe comandos."
                          if action != "save_whatsapp_notest" else "WhatsApp guardado sem teste.", "ok")
        else:
            abort(400)
        return redirect(url_for("config", cat="alertas"))

    @app.post("/configuracao/reiniciar-portao")
    def reset_gate_route():
        if request.form.get("confirmar", "").strip() != "REINICIAR":
            flash('Escreve exatamente "REINICIAR" para confirmar — isto apaga os bots da Testnet.', "error")
            return redirect(url_for("config", cat="sistema"))
        result = readiness.reset_gate(conn(), now_ms())
        if not result["ok"]:
            flash("Não posso reiniciar: pára primeiro estes bots da Testnet (PARAR TUDO ou um a um): " +
                  ", ".join(result["blocked"]) + ".", "error")
        else:
            db.log(conn(), "Portão da conta real reiniciado", f"{result['deleted']} bot(s) da Testnet apagado(s)")
            flash(f"Portão reiniciado. {result['deleted']} bot(s) da Testnet apagado(s); a contagem recomeça agora.", "ok")
        return redirect(url_for("config", cat="sistema"))

    def _merge(cfg, form):
        merged = dict(cfg)
        for k in cfg:
            if k != "exclusions" and k in form:
                merged[k] = form[k]
        merged["exclusions"] = ",".join(form.getlist("exclusions"))
        return merged

    @app.route("/parar-tudo", methods=["GET", "POST"])
    def stop_all():
        if request.method == "POST":
            db.set_many(conn(), {"emergency_stop": "1"})
            db.log(conn(), "PARAR TUDO", "Todos os bots parados e ordens abertas canceladas.")
            flash("Paragem pedida. Os bots estão a parar: cada um só aparece como parado depois de a Testnet o confirmar.", "ok")
            return redirect(url_for("inicio"))
        active = [r for r in botstore.all_bots(conn()) if r["status"] != "stopped"]
        n_orders = sum(conn().execute("SELECT COUNT(*) FROM bot_orders WHERE bot_id = ?", (r["id"],)).fetchone()[0]
                       for r in active)
        return render_template("stop_confirm.html", resume=False, n_bots=len(active), n_orders=n_orders)

    @app.route("/reativar", methods=["GET", "POST"])
    def resume():
        if request.method == "POST":
            db.set_many(conn(), {"emergency_stop": "0"})
            db.log(conn(), "Sistema reativado", "Saiu do modo seguro.")
            flash("Sistema reativado. Os bots continuam parados até os ligares.", "ok")
            return redirect(url_for("inicio"))
        return render_template("stop_confirm.html", resume=True)

    @app.get("/alertas")
    def alerts_page():
        return render_template("alerts.html", rows=db.recent_log(conn()), open_alerts=alerts.open_alerts(conn()))

    @app.post("/alertas/visto")
    def alert_seen():
        alerts.mark_seen(conn(), request.form.get("key", ""))
        return redirect(request.referrer or url_for("alerts_page"))

    @app.get("/mais")
    def more():
        return render_template("mais.html")

    # ---------- portefólio real (só leitura) ----------
    def key_file():
        return app.config["KEY_FILE"] or keystore.default_path()

    def make_reader(key, secret):
        return (app.config["READER_FACTORY"] or BinanceReader)(key, secret)

    def real_trading_key_file():
        return app.config["REAL_TRADING_KEY_FILE"] or keystore.real_trading_path()

    def load_snapshot():
        raw = db.get(conn(), "portfolio_snapshot")
        return json.loads(raw) if raw else None

    def refresh_portfolio(force=False):
        creds = keystore.load(key_file())
        if not creds:
            return {"state": "no_key", "snap": None}
        snap = load_snapshot()
        if snap and not force and not db.get(conn(), "portfolio_error"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["ts"])).total_seconds()
            if age < 60:
                return {"state": "ok", "snap": snap}
        try:
            new = portfolio.build_snapshot(make_reader(*creds))
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BinanceError) else "erro inesperado na leitura."
            alerts.connection_failed(conn(), reason)
            db.set_many(conn(), {"portfolio_error": reason})
            return {"state": "error", "snap": snap, "reason": reason}
        db.set_many(conn(), {"portfolio_snapshot": json.dumps(new), "portfolio_error": ""})
        alerts.connection_ok(conn())
        alerts.sync(conn(), risk.evaluate(new["holdings"], risk.thresholds(db.get_all(conn()))))
        return {"state": "ok", "snap": new}

    def rate_for(snap, cur):
        return {"USDT": 1.0, "USD": 1.0, "EUR": snap.get("eur_per_usdt")}[cur]

    @app.get("/portefolio")
    def portfolio_page():
        result = refresh_portfolio()
        snap = result["snap"]
        cur = session.get("cur", "USDT")
        sort = request.args.get("ordem", "valor")
        flt = request.args.get("filtro", "")
        show_dust = request.args.get("po") == "1"
        view, costs = None, costbasis.load(conn())
        open_alerts = [a for a in alerts.open_alerts(conn()) if a["criterion"] != "ligacao"]
        if snap:
            holdings = costbasis.apply([dict(h) for h in snap["holdings"]], costs)
            view = layout.build(snap, holdings, rate_for(snap, cur), sort, flt, show_dust, open_alerts)
            for sec in view["sections"]:
                for r in sec["rows"]:
                    r["warning"] = costbasis.loss_warning(r)
        return render_template("portfolio.html", result=result, snap=snap, view=view, costs=costs,
                               risks=open_alerts, show_dust=show_dust, sort=sort, flt=flt,
                               sorts=layout.SORTS, filters=layout.FILTERS)

    @app.post("/portefolio/custo")
    def cost_basis_edit():
        coin = request.form.get("coin", "").strip().upper()
        if request.form.get("action") == "delete":
            costbasis.delete(conn(), coin)
            db.log(conn(), "Custo médio removido", coin)
            flash(f"Custo médio de {coin} removido.", "ok")
        else:
            price = costbasis.parse_price(request.form.get("price"))
            if not coin.isalnum() or price is None:
                flash("Escreve a moeda (ex.: ADA) e um preço médio maior que zero (ex.: 0,75).", "error")
            else:
                costbasis.save(conn(), coin, price)
                db.log(conn(), "Custo médio guardado", f"{coin}: {price:g} USDT")
                flash(f"Custo médio de {coin} guardado: {price:g} USDT.", "ok")
        return redirect(url_for("portfolio_page") + "#custo")

    @app.post("/portefolio/atualizar")
    def portfolio_refresh():
        refresh_portfolio(force=True)
        return redirect(url_for("portfolio_page"))

    # ---------- staking (Simple Earn, só leitura) ----------
    def refresh_staking(force=False):
        raw = db.get(conn(), "staking_snapshot")
        snap = json.loads(raw) if raw else None
        creds = keystore.load(key_file())
        if not creds:
            return {"state": "no_key", "snap": None}
        if snap and not force and not db.get(conn(), "staking_error"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(snap["ts"])).total_seconds()
            if age < 300:
                return {"state": "ok", "snap": snap}
        pf = load_snapshot()
        held = [h["coin"] for h in pf["holdings"]] if pf else []
        try:
            new = staking.build_snapshot(make_reader(*creds), held)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, BinanceError) else "erro inesperado na leitura do Earn."
            db.set_many(conn(), {"staking_error": reason})
            return {"state": "error", "snap": snap, "reason": reason}
        db.set_many(conn(), {"staking_snapshot": json.dumps(new), "staking_error": ""})
        return {"state": "ok", "snap": new}

    def risk_by_coin():
        out = {}
        for a in alerts.open_alerts(conn()):
            if a["criterion"] != "ligacao" and (a["coin"] not in out or RANK[a["severity"]] > RANK[out[a["coin"]]]):
                out[a["coin"]] = a["severity"]
        return out

    def staking_model(result):
        pf = load_snapshot()
        if not (result["snap"] and pf):
            return None, None
        prices = {h["coin"]: h["price"] for h in pf["holdings"] if h.get("priced")}
        summary = staking.positions_summary(result["snap"], prices)
        sugs = staking.suggestions(result["snap"], pf["holdings"], risk_by_coin())
        return summary, sugs

    @app.get("/staking")
    def staking_page():
        result = refresh_staking()
        summary, sugs = staking_model(result)
        pf = load_snapshot()
        cur = session.get("cur", "USDT")
        rate = rate_for(pf, cur) if pf else None
        return render_template("staking.html", result=result, summary=summary, sugs=sugs, rate=rate)

    @app.post("/staking/atualizar")
    def staking_refresh():
        refresh_staking(force=True)
        return redirect(url_for("staking_page"))

    # ---------- Ai: análise completa + resumo preparado para uma IA futura ----------
    def _ai_chat_url():
        return db.get(conn(), "ai_chat_url") or ""

    @app.get("/ai")
    def ai_page():
        pf_result = refresh_portfolio()
        pf = pf_result["snap"]
        if not pf:
            return render_template("ai.html", pf=None, result=pf_result, ai_chat_url=_ai_chat_url())
        st_result = refresh_staking()
        summary, sugs = staking_model(st_result)
        holdings = costbasis.apply([dict(h) for h in pf["holdings"]], costbasis.load(conn()))
        open_alerts = [a for a in alerts.open_alerts(conn()) if a["criterion"] != "ligacao"]
        report = analysis.build(pf, holdings, summary, sugs or [], open_alerts)
        text = analysis.summary_text(analysis.ai_summary(pf, holdings, summary, open_alerts))
        secrets_ = list(keystore.load(key_file()) or ())
        problems = analysis.verify_no_secrets(text, secrets_)
        cur = session.get("cur", "USDT")
        rate = rate_for(pf, cur)
        return render_template("ai.html", pf=pf, result=pf_result, report=report, rate=rate,
                               summary_text=None if problems else text, problems=problems, ai_chat_url=_ai_chat_url())

    @app.post("/ai/link")
    def ai_link():
        action = request.form.get("action", "save")
        if action == "clear":
            db.set_many(conn(), {"ai_chat_url": ""})
            db.log(conn(), "Link da IA removido")
            flash("Link removido.", "ok")
            return redirect(url_for("ai_page"))
        url = request.form.get("url", "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            flash("Esse link não parece válido. Tem de começar por http:// ou https://.", "error")
        else:
            db.set_many(conn(), {"ai_chat_url": url})
            db.log(conn(), "Link da IA guardado", parsed.netloc)
            flash("Link guardado.", "ok")
        return redirect(url_for("ai_page"))

    # ---------- Validação do projeto: checklist com evidência real (nunca inventada) ----------
    @app.get("/validacao")
    def validation_page():
        return render_template("validacao.html", v=validation.view(conn()), running=validation.is_running())

    @app.post("/validacao/correr")
    def validation_run():
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if validation.is_running():
            if is_ajax:
                return jsonify(started=False, error="Já há uma validação a correr."), 409
            flash("Já há uma validação a correr.", "error")
            return redirect(url_for("validation_page"))
        modo = "tudo" if request.form.get("modo") == "tudo" else "pendentes"
        db_path = app.config["DB_PATH"]
        validation.begin()

        def work():
            c = db.connect(db_path)
            try:
                validation.run(persist_conn=c, mode=modo)
            except Exception:
                log.exception("validação em segundo plano falhou")
                validation.mark_idle()
            finally:
                c.close()
        threading.Thread(target=work, daemon=True).start()
        if is_ajax:
            return jsonify(started=True)
        flash("Validação a começar em segundo plano. Atualiza a página daqui a pouco para ver o resultado.", "ok")
        return redirect(url_for("validation_page"))

    @app.get("/validacao/progresso")
    def validation_progress():
        return jsonify(validation.progress_snapshot())

    # ---------- Reserva: o algoritmo propõe, tu decides; nunca compra nada ----------
    @app.get("/reserva")
    def reserva():
        cfg = db.get_all(conn())
        split = float(cfg["split_reserve"])
        v = reserve.view(conn(), snapshot=load_snapshot(), split_reserve_pct=split)
        return render_template("reserva.html", v=v, split_reserve=cfg["split_reserve"])

    @app.post("/reserva/gerar")
    def reserva_gerar():
        try:
            tickers = make_reader("", "").tickers_all()
        except BinanceError as exc:
            flash(f"Não consegui ler o mercado agora ({exc}). Tenta de novo daqui a pouco.", "error")
            return redirect(url_for("reserva"))
        exclusions = [c for c in db.get(conn(), "exclusions").split(",") if c]
        created = reserve.propose(conn(), tickers, exclusions)
        if created:
            db.log(conn(), "Propostas da reserva geradas", ", ".join(c["coin"] for c in created))
            flash(f"{len(created)} proposta(s) nova(s).", "ok")
        else:
            flash("Nenhuma moeda nova cumpre os critérios agora (ou já foram todas propostas antes).", "ok")
        return redirect(url_for("reserva"))

    @app.post("/reserva/decidir")
    def reserva_decidir():
        pid = request.form.get("id", type=int)
        action = request.form.get("action")
        note = request.form.get("note", "").strip()
        if pid is None or action not in ("approve", "reject"):
            abort(400)
        result = reserve.decide(conn(), pid, action == "approve", note)
        if result is None:
            flash("Essa proposta já não está pendente.", "error")
        else:
            verb = "aprovada" if result["status"] == "approved" else "rejeitada"
            db.log(conn(), f"Proposta da reserva {verb}", result["coin"])
            flash(f"{result['coin']}: proposta {verb}.", "ok")
        return redirect(url_for("reserva"))

    @app.post("/reserva/remover")
    def reserva_remover():
        pid = request.form.get("id", type=int)
        note = request.form.get("note", "").strip()
        if pid is None:
            abort(400)
        result = reserve.remove(conn(), pid, note)
        if result is None:
            flash("Essa moeda já não está na reserva.", "error")
        else:
            db.log(conn(), "Moeda removida da reserva", result["coin"])
            flash(f"{result['coin']}: retirada da reserva.", "ok")
        return redirect(url_for("reserva"))

    @app.post("/reserva/ajustar")
    def reserva_ajustar():
        pid = request.form.get("id", type=int)
        pct = request.form.get("pct", type=float)
        if pid is None or pct is None:
            abort(400)
        result = reserve.adjust(conn(), pid, pct)
        if result is None:
            flash("Peso inválido, ou essa moeda já não está na reserva. Tem de estar entre 0 e 100%.", "error")
        else:
            db.log(conn(), "Peso da reserva ajustado", f"{result['coin']}: {result['pct']:g}%")
            flash(f"{result['coin']}: peso-alvo passou a {result['pct']:g}%.", "ok")
        return redirect(url_for("reserva"))

    @app.route("/portefolio/chave", methods=["GET", "POST"])
    def portfolio_key():
        creds = keystore.load(key_file())
        if request.method == "POST":
            action = request.form.get("action")
            if action == "delete":
                keystore.delete(key_file())
                db.set_many(conn(), {"portfolio_snapshot": ""})
                db.log(conn(), "Chave da Binance removida")
                flash("Chave removida.", "ok")
                return redirect(url_for("portfolio_key"))
            if action == "save":
                key, secret = request.form.get("api_key", "").strip(), request.form.get("api_secret", "").strip()
                if not key or not secret:
                    flash("Preenche a chave e o segredo.", "error")
                    return redirect(url_for("portfolio_key"))
            else:  # testar a chave já guardada
                if not creds:
                    flash("Ainda não há chave guardada.", "error")
                    return redirect(url_for("portfolio_key"))
                key, secret = creds
            try:
                restrictions = make_reader(key, secret).restrictions()
            except BinanceError as exc:
                flash(str(exc), "error")
                return redirect(url_for("portfolio_key"))
            ok, problems, warnings = check_permissions(restrictions)
            if not ok:
                flash("Chave recusada: " + "; ".join(problems) + ". Cria na Binance uma chave só de leitura.", "error")
                return redirect(url_for("portfolio_key"))
            if action == "save":
                keystore.save(key_file(), key, secret)
                db.log(conn(), "Chave da Binance guardada (só leitura)", keystore.masked(key))
            for w in warnings:
                flash(w, "error")
            flash("Ligação bem-sucedida. A chave é só de leitura." if action != "save"
                  else "Chave guardada. É só de leitura.", "ok")
            return redirect(url_for("portfolio_page") if action == "save" else url_for("portfolio_key"))
        return render_template("portfolio_key.html", masked=keystore.masked(creds[0]) if creds else None)

    # ---------- conta real: só infraestrutura e guardas; nenhum bot negoceia com isto ainda ----------
    CONFIRM_PHRASE = "ATIVAR DINHEIRO REAL"

    @app.route("/conta-real", methods=["GET", "POST"])
    def real_account():
        cfg = db.get_all(conn())
        enabled = cfg["real_trading_enabled"] == "1"
        creds = keystore.load(real_trading_key_file())
        gate = readiness.evaluate(conn(), now_ms())
        if request.method == "POST":
            action = request.form.get("action")
            if action == "guardar":
                key, secret = request.form.get("api_key", "").strip(), request.form.get("api_secret", "").strip()
                if not key or not secret:
                    flash("Preenche a chave e o segredo.", "error")
                    return redirect(url_for("real_account"))
                try:
                    restrictions = make_reader(key, secret).restrictions()
                except BinanceError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("real_account"))
                ok, problems, warnings = check_trading_permissions(restrictions)
                if not ok:
                    flash("Chave recusada: " + "; ".join(problems) + ". Cria na Binance uma chave só com negociação "
                          "spot — nunca levantamentos, margem ou futuros.", "error")
                    return redirect(url_for("real_account"))
                keystore.save(real_trading_key_file(), key, secret)
                db.log(conn(), "Chave de negociação real guardada", keystore.masked(key))
                for w in warnings:
                    flash(w, "error")
                flash("Chave guardada. Continua sem enviar ordens: falta ativar (e cumprir o portão).", "ok")
                return redirect(url_for("real_account"))
            if action == "testar":
                if not creds:
                    flash("Ainda não há chave guardada.", "error")
                    return redirect(url_for("real_account"))
                try:
                    restrictions = make_reader(*creds).restrictions()
                except BinanceError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("real_account"))
                ok, problems, warnings = check_trading_permissions(restrictions)
                if not ok:
                    flash("A chave guardada já não cumpre os requisitos: " + "; ".join(problems) + ".", "error")
                else:
                    for w in warnings:
                        flash(w, "error")
                    flash("Ligação bem-sucedida. A chave continua dentro do que é permitido.", "ok")
                return redirect(url_for("real_account"))
            if action == "remover":
                keystore.delete(real_trading_key_file())
                if enabled:
                    db.set_many(conn(), {"real_trading_enabled": "0"})
                db.log(conn(), "Chave de negociação real removida")
                flash("Chave removida.", "ok")
                return redirect(url_for("real_account"))
            if action == "ativar":
                if not creds:
                    flash("Guarda primeiro uma chave de negociação válida.", "error")
                elif not gate["ready"]:
                    flash("O portão ainda não está cumprido (dias, ciclos ou comparação com comprar-e-manter). "
                          "Não é possível ativar.", "error")
                elif request.form.get("confirmar", "").strip() != CONFIRM_PHRASE:
                    flash(f'Escreve exatamente "{CONFIRM_PHRASE}" para confirmar.', "error")
                else:
                    db.set_many(conn(), {"real_trading_enabled": "1"})
                    db.log(conn(), "Conta real ATIVADA")
                    flash("Ativada. (Nota: ainda não existe nenhum fluxo para criar um bot real — isso é a fase "
                          "seguinte.)", "ok")
                return redirect(url_for("real_account"))
            if action == "desativar":
                db.set_many(conn(), {"real_trading_enabled": "0"})
                db.log(conn(), "Conta real desativada")
                flash("Desativada.", "ok")
                return redirect(url_for("real_account"))
            abort(400)
        return render_template("conta_real.html", masked=keystore.masked(creds[0]) if creds else None,
                              enabled=enabled, gate=gate, confirm_phrase=CONFIRM_PHRASE)

    def placeholder(key):
        def view():
            title, text = PLACEHOLDERS[key]
            return render_template("placeholder.html", title=title, text=text)
        view.__name__ = key
        return view

    for key in PLACEHOLDERS:
        app.add_url_rule(f"/{key}", key, placeholder(key))

    # ---------- bots de grelha (só simulação) ----------
    def now_ms():
        return int(time.time() * 1000)

    def runner_state():
        beat = db.get(conn(), "runner_heartbeat")
        if not beat:
            return {"alive": False, "text": "O corredor dos bots nunca correu."}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds()
        return {"alive": age < 180, "text": f"Última atividade há {int(age)} s." if age < 180
                else f"O corredor parou há {int(age // 60)} min. Os bots não avançam até ele voltar."}

    def testnet_file():
        return app.config["TESTNET_KEY_FILE"] or keystore.testnet_path()

    def telegram_file():
        return app.config["TELEGRAM_FILE"] or notify.TELEGRAM_FILE or keystore.telegram_path()

    def whatsapp_file():
        return app.config["WHATSAPP_FILE"] or notify.WHATSAPP_FILE or keystore.whatsapp_path()

    def make_trader(key=None, secret=None):
        if key is None:
            creds = keystore.load(testnet_file())
            key, secret = creds if creds else ("", "")
        factory = app.config["TRADER_FACTORY"]
        return factory(key, secret) if factory else Trader(key, secret)

    @app.route("/bots/ligacoes", methods=["GET", "POST"])
    def bot_links():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "delete_testnet":
                keystore.delete(testnet_file())
                db.log(conn(), "Chaves da Testnet removidas")
                flash("Chaves da Testnet removidas.", "ok")
            elif action == "save_testnet":
                key, secret = request.form.get("api_key", "").strip(), request.form.get("api_secret", "").strip()
                if not key or not secret:
                    flash("Preenche a chave e o segredo da Testnet.", "error")
                else:
                    try:
                        make_trader(key, secret).account()
                    except TraderError as exc:
                        flash(str(exc), "error")
                    else:
                        keystore.save(testnet_file(), key, secret)
                        db.log(conn(), "Chaves da Testnet guardadas", keystore.masked(key))
                        flash("Ligação bem-sucedida. Chaves da Testnet guardadas (não servem na conta real).", "ok")
            return redirect(url_for("bot_links"))
        tn = keystore.load(testnet_file())
        return render_template("bots_links.html", testnet=keystore.masked(tn[0]) if tn else None)

    def bot_params():
        cfg = db.get_all(conn())
        return {"max_loss_trade_pct": float(cfg["max_loss_trade"]), "pause_drawdown_pct": float(cfg["pause_drawdown"])}

    def pair_selection(tickers, only=None):
        """Sugestões com os critérios das definições e, se nada cumprir, o plano B (ver pairs.select)."""
        cfg = db.get_all(conn())
        excl = [c for c in cfg["exclusions"].split(",") if c]
        return pairs.select(tickers, excl or ("stables",), pairs.params_from(cfg), only=only,
                            adaptive=cfg.get("pair_adaptive", "1") != "0")

    def suggestions_cached(force=False):
        cache = app.extensions.setdefault("pair_cache", {"t": 0, "data": None, "error": None})
        if force or cache["data"] is None or time.time() - cache["t"] > 300:
            try:
                tickers = make_reader("", "").tickers_all()
                sel = pair_selection(tickers)
                cache.update(t=time.time(), data=sel["items"], note=sel["note"], level=sel["level"],
                             criteria=sel["criteria"], error=None, tickers=tickers)
            except Exception as exc:
                cache.update(t=time.time(), data=[], note="", level=0, error=str(exc) if isinstance(exc, BinanceError) else "sem dados",
                             tickers={})
        return cache

    def default_capital():
        snap = load_snapshot()
        return round(snap["total_usdt"] * 0.15, 2) if snap else 90.0

    def preview_grid(pair, capital, mode="sim"):
        """Monta a grelha com o preço e as regras reais do par. Levanta GridRefused se a guarda recusar.

        Devolve (preço, regras, parâmetros, grelha, capital, nota). Na Testnet, se o saldo livre em USDT não chega para
        o orçamento da grelha, o capital é ajustado ao que a conta tem (a nota explica) em vez de recusar.
        """
        note = ""

        def pt(x):
            return f"{x:,.2f}".replace(",", " ").replace(".", ",")

        if mode == "testnet":
            trader = make_trader()
            if not trader.has_keys:
                raise G.GridRefused("Faltam as chaves da Testnet. Guarda-as em Bots > Ligações.")
            rules = trader.symbol_rules(pair)
            if rules.get("status") != "TRADING":
                raise G.GridRefused(f"O par {pair} não está em negociação na Testnet.")
            price = float(trader.ticker24(pair)["lastPrice"])
            params = bot_params()
            free = trader.free_balance("USDT")
            grid = G.build_grid(price, capital, rules, params)
            if free < grid["budget"]:                       # o orçamento da grelha (sem a reserva de caixa) não cabe
                reserve = 1 - {**G.DEFAULTS, **params}["cash_reserve_pct"] / 100
                adjusted = int(free * 0.97 / reserve * 100) / 100     # 3% de folga para comissões e mexidas de preço
                try:
                    grid = G.build_grid(price, adjusted, rules, params)
                except G.GridRefused:
                    raise G.GridRefused(
                        f"A conta da Testnet só tem {pt(free)} USDT livres, pouco para uma grelha segura neste par. "
                        "Pára ou apaga outros bots da Testnet que estejam a usar o saldo, ou repõe o saldo da conta "
                        "no site da Testnet da Binance.") from None
                note = (f"A conta da Testnet só tem {pt(free)} USDT livres (a Testnet não deixa depositar mais por aqui) "
                        f"e a grelha precisava de {pt(G.build_grid(price, capital, rules, params)['budget'])}. Por isso o "
                        f"capital foi ajustado de {capital:g} para {adjusted:g} USDT, nos dois bots. Se preferires outro "
                        "valor, cancela e escolhe um capital mais baixo.")
                capital = adjusted
            return price, rules, params, grid, capital, note
        reader = make_reader("", "")
        rules = reader.symbol_rules(pair)
        if rules.get("status") != "TRADING":
            raise G.GridRefused(f"O par {pair} não está em negociação normal na Binance.")
        price = float(reader.ticker24(pair)["lastPrice"])
        params = bot_params()
        return price, rules, params, G.build_grid(price, capital, rules, params), capital, note

    def testnet_pairs(cache, force=False):
        """Sugestões reais filtradas para os pares que existem na Testnet."""
        store = app.extensions.setdefault("testnet_syms", {"t": 0, "data": None})
        if force or store["data"] is None or time.time() - store["t"] > 300:
            store.update(t=time.time(), data=make_trader().trading_symbols())
        sel = pair_selection(cache.get("tickers") or {}, only=store["data"])   # os critérios aplicam-se só ao que existe na Testnet
        cache.update(note=sel["note"], level=sel["level"], criteria=sel["criteria"])
        return pairs.for_testnet(sel["items"], store["data"])

    BOT_TABS = {"real": None, "simulacao": "sim", "testnet": "testnet"}

    @app.get("/bots")
    def bots():
        """Bots em três separadores: real (ainda não existe), simulação e Testnet."""
        rows = []
        for r in botstore.all_bots(conn()):
            eng = botstore.load_engine(conn(), r["id"])
            rows.append({"row": r, "stats": botstore.stats(eng, now_ms()), "eng": eng})
        counts = {"real": 0, "simulacao": sum(1 for it in rows if it["row"]["mode"] == "sim"),
                  "testnet": sum(1 for it in rows if it["row"]["mode"] == "testnet")}
        aba = request.args.get("aba")
        if aba not in BOT_TABS:
            aba = "testnet" if counts["testnet"] else "simulacao"
        shown = [it for it in rows if it["row"]["mode"] == BOT_TABS[aba]]
        tabs = [("real", "Real", counts["real"]), ("simulacao", "Simulação", counts["simulacao"]),
                ("testnet", "Testnet", counts["testnet"])]
        active_capital = sum(it["row"]["capital_usdt"] for it in rows if it["row"]["status"] != "stopped")
        usage = balance.trading_usage(active_capital, load_snapshot(), float(db.get_all(conn())["split_trading"]))
        gate = readiness.evaluate(conn(), now_ms()) if aba == "real" else None
        return render_template("bots.html", rows=shown, runner=runner_state(), tabs=tabs, aba=aba, usage=usage, gate=gate,
                               n_deletable=sum(1 for it in shown if botstore.can_delete(conn(), it["row"])))

    @app.route("/bots/novo", methods=["GET", "POST"])
    def bot_new():
        mode = request.values.get("modo", "sim")
        mode = mode if mode in ("sim", "testnet") else "sim"
        cache = dict(suggestions_cached(force=request.args.get("atualizar") == "1"))
        if mode == "testnet":
            try:
                cache["data"] = testnet_pairs(cache, force=request.args.get("atualizar") == "1")
            except TraderError as exc:
                cache.update(data=[], error=f"Testnet: {exc}")
        if request.method == "POST":
            pair = request.form.get("pair", "")
            twin = request.form.get("twin") == "1"                     # duplicado no outro tipo (sim <-> Testnet)
            never_below = request.form.get("never_below_cost") == "1"
            capital = db.parse_decimal_pt(request.form.get("capital")) or 0.0
            chosen = next((s for s in cache["data"] if s["pair"] == pair), None)
            if not chosen or capital <= 0:
                flash("Escolhe um dos pares sugeridos e indica um capital maior que zero.", "error")
                return redirect(url_for("bot_new", modo=mode))
            if capital > MAX_BOT_CAPITAL:
                flash(f"Capital acima do limite ({MAX_BOT_CAPITAL:,.0f} USDT). Confirma que não foi engano.", "error")
                return redirect(url_for("bot_new", modo=mode))
            try:
                if twin and mode == "sim":                              # o duplicado na Testnet obriga o par a existir lá
                    if pair not in make_trader().trading_symbols():
                        flash(f"O par {pair} não existe na Testnet, por isso não dá para criar o duplicado. "
                              "Desmarca a opção ou escolhe outro par.", "error")
                        return redirect(url_for("bot_new", modo=mode))
                # com duplicado, os dois bots usam o preço e as regras da Testnet (comparação justa)
                price, rules, params, g, capital, funding_note = preview_grid(pair, capital, "testnet" if twin else mode)
            except (G.GridRefused, BinanceError, TraderError) as exc:
                flash(str(exc), "error")
                return redirect(url_for("bot_new", modo=mode))
            params = {**params, "never_sell_below_cost": never_below}   # opcional por bot, desligada por defeito
            if request.form.get("step") == "create":
                if twin:                                    # o par: um bot na Testnet e o gémeo em simulação, mesmas velas
                    tn_id = botstore.create(conn(), pair, capital, rules, params, mode="testnet")
                    sim_id = botstore.create(conn(), pair, capital, rules, params, mode="sim", source="testnet",
                                             twin_of=tn_id)
                    bid = tn_id if mode == "testnet" else sim_id
                else:
                    bid = botstore.create(conn(), pair, capital, rules, params, mode=mode)
                db.log(conn(), f"Bot criado ({'Testnet' if mode == 'testnet' else 'simulação'})",
                       f"{pair}, capital {capital:g} USDT{' + duplicado no outro tipo (Testnet e simulação)' if twin else ''}")
                flash("Bot criado e aprovado. O corredor arranca-o no próximo minuto. "
                      + ("Criei também o duplicado, para os comparares em Estatísticas: um na Testnet (ordens reais na "
                         "conta fictícia) e outro em simulação, com as mesmas velas." if twin else
                         "Envia ordens só para a Testnet (dinheiro fictício)." if mode == "testnet"
                         else "É só simulação."), "ok")
                return redirect(url_for("bot_detail", bot_id=bid))
            return render_template("bot_preview.html", chosen=chosen, capital=capital, price=price, rules=rules, g=g,
                                   p={**G.DEFAULTS, **params}, cost=G.cost_pct(), mode=mode, twin=twin,
                                   never_below=never_below, note=funding_note)
        return render_template("bot_new.html", cache=cache, capital=default_capital(), mode=mode)

    @app.get("/bots/<int:bot_id>")
    def bot_detail(bot_id):
        eng = botstore.load_engine(conn(), bot_id)
        if eng is None:
            abort(404)
        window = request.args.get("janela", "24h")
        window = window if window in chart.WINDOWS else "24h"
        since = (eng.last_ts or now_ms()) - chart.WINDOWS[window] * 3_600_000
        price_chart = chart.build(botstore.candles(conn(), bot_id, since), eng.grid,
                                  botstore.fills_since(conn(), bot_id, since),
                                  botstore.events_since(conn(), bot_id, since), eng.orders, window)
        curve = botstore.equity_curve(conn(), bot_id)
        equity = chart_path(curve)
        return render_template("bot_detail.html", bot=botstore.get(conn(), bot_id), eng=eng,
                               stats=botstore.stats(eng, now_ms(), curve), events=botstore.events(conn(), bot_id),
                               fills=botstore.fills(conn(), bot_id), runner=runner_state(),
                               pchart=price_chart, window=window, windows=list(chart.WINDOWS), equity=equity)

    @app.route("/bots/<int:bot_id>/apagar", methods=["GET", "POST"])
    def bot_delete(bot_id):
        row = botstore.get(conn(), bot_id)
        if row is None:
            abort(404)
        if not botstore.can_delete(conn(), row):
            flash("Só se apaga um bot parado e sem ordens por fechar. Pára-o primeiro.", "error")
            return redirect(url_for("bot_detail", bot_id=bot_id))
        eng = botstore.load_engine(conn(), bot_id)
        if request.method == "POST":
            if botstore.delete(conn(), bot_id):
                db.log(conn(), "Bot apagado", f"Bot {bot_id} {row['pair']} ({row['mode']})")
                flash(f"Bot {row['pair']} apagado. Os dados dele deixaram de estar guardados.", "ok")
            else:                              # deixou de poder apagar-se entre mostrar a confirmação e submeter
                flash("Já não dá para apagar este bot (deixou de estar parado ou ficou com algo por fechar). "
                      "Verifica o estado dele.", "error")
            return redirect(url_for("bots", aba="testnet" if row["mode"] == "testnet" else "simulacao"))
        return render_template("bot_delete_confirm.html", bots=[(row, eng)], aba="", back=url_for("bot_detail", bot_id=bot_id))

    @app.route("/bots/apagar-parados", methods=["GET", "POST"])
    def bots_delete_stopped():
        aba = request.values.get("aba", "")
        mode = BOT_TABS.get(aba)                     # só os bots deste separador (sem separador: todos os parados)
        rows = [r for r in botstore.deletable(conn()) if mode is None or r["mode"] == mode]
        back = url_for("bots", aba=aba) if aba in BOT_TABS else url_for("bots")
        if request.method == "POST":
            done = [r for r in rows if botstore.delete(conn(), r["id"])]
            db.log(conn(), "Bots parados apagados", ", ".join(f"{r['id']} {r['pair']}" for r in done) or "nenhum")
            n = len(done)
            msg = f"{n} bot{'' if n == 1 else 's'} parado{'' if n == 1 else 's'} apagado{'' if n == 1 else 's'}."
            if n < len(rows):
                msg += f" {len(rows) - n} já não puderam ser apagados (deixaram de estar parados entretanto)."
            flash(msg, "ok" if n == len(rows) else "error")
            return redirect(back)
        if not rows:
            flash("Não há bots parados para apagar.", "ok")
            return redirect(back)
        return render_template("bot_delete_confirm.html", bots=[(r, botstore.load_engine(conn(), r["id"])) for r in rows],
                               aba=aba, back=back)

    @app.get("/bots/<int:bot_id>/parar")
    def bot_stop_confirm(bot_id):
        eng = botstore.load_engine(conn(), bot_id)
        if eng is None:
            abort(404)
        return render_template("bot_stop_confirm.html", bot=botstore.get(conn(), bot_id), eng=eng)

    @app.post("/bots/<int:bot_id>/comando")
    def bot_command(bot_id):
        cmd = request.form.get("cmd")
        if cmd == "stop" and request.form.get("confirm") != "1":       # parar um bot pede sempre confirmação
            return redirect(url_for("bot_stop_confirm", bot_id=bot_id))
        row = botstore.get(conn(), bot_id)
        if cmd == "activate" and row is not None:                        # ATIVAR: um bot de cada vez, nunca automático
            eng = botstore.load_engine(conn(), bot_id)
            if db.get(conn(), "emergency_stop") == "1":
                flash("O sistema ainda está em modo seguro. Carrega em Reativar na barra de cima; depois ativa os bots um a um.", "error")
            elif row["status"] != "stopped" or botstore.has_pending(conn(), row):
                flash("Este bot ainda está a parar ou tem operações por confirmar na exchange. Tenta daqui a pouco.", "error")
            elif eng.s.get("testnet_reset") or not eng.grid:
                flash("Este bot não pode voltar a arrancar (a Testnet foi reposta ou nunca chegou a começar). Cria um bot novo.", "error")
            else:
                botstore.set_command(conn(), bot_id, cmd)
                db.log(conn(), f"Pedido ao bot {bot_id}: ativar")
                flash("Pedido enviado. O corredor ativa o bot no próximo minuto.", "ok")
            return redirect(url_for("bot_detail", bot_id=bot_id))
        if cmd in ("pause", "resume", "stop") and row:
            botstore.set_command(conn(), bot_id, cmd)
            db.log(conn(), f"Pedido ao bot {bot_id}: {cmd}")
            flash("Pedido enviado. O corredor aplica-o no próximo minuto.", "ok")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    @app.get("/estatisticas")
    def estatisticas():
        cur = session.get("cur", "USDT")
        pf = load_snapshot()
        rate = rate_for(pf, cur) if pf else (1.0 if cur != "EUR" else None)
        items = []
        for r in botstore.all_bots(conn()):
            eng = botstore.load_engine(conn(), r["id"])
            st = botstore.stats(eng, now_ms())
            curve = botstore.equity_curve(conn(), r["id"])
            items.append({"row": r, "stats": st, "chart": chart_path(curve)})
        by_id = {it["row"]["id"]: it for it in items}
        compare = [{"testnet": by_id[it["row"]["twin_of"]], "sim": it} for it in items
                   if it["row"]["twin_of"] and it["row"]["twin_of"] in by_id]
        return render_template("stats.html", items=items, rate=rate, runner=runner_state(), compare=compare)

    def chart_path(curve, w=560, h=180):
        if len(curve) < 2:
            return None
        xs, ys = [c[0] for c in curve], [c[1] for c in curve]
        lo, hi = min(ys), max(ys)
        span = (hi - lo) or 1.0
        pts = [(40 + (x - xs[0]) / ((xs[-1] - xs[0]) or 1) * (w - 60), h - 30 - (y - lo) / span * (h - 50))
               for x, y in zip(xs, ys)]
        return {"line": " ".join(f"{x:.1f},{y:.1f}" for x, y in pts), "lo": lo, "hi": hi, "w": w, "h": h,
                "end": pts[-1]}

    return app
