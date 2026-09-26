"""Malha, v0.1: painel com palavra-passe, configuração, preços e PARAR TUDO.

Só modo simulação. Nada aqui envia ordens.
"""
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import alerts, analysis, botstore, chart, config as cfgmod, explain, costbasis, db, keystore, layout, market, notify, pairs, portfolio, risk, staking
from . import grid as G
from . import recommendations as rec
from .reader import BinanceError, BinanceReader, check_permissions
from .trader import Trader, TraderError
from .risk import RANK
from .rules import EXCLUSION_CATEGORIES, RISK_PAIRS, describe_changes, parse_config

CURRENCIES = ("USDT", "EUR", "USD")
PUBLIC_ENDPOINTS = {"login", "setup", "static"}
MAX_FAILS, FAIL_WINDOW = 5, 300
# estado do bot: texto com símbolo (a cor nunca é o único sinal) e classe da pílula; "parado" não é erro
STATES = {"running": ("● A trabalhar", "gain"), "paused": ("⏸ Em pausa", "warn"),
          "pending": ("◌ A arrancar", "warn"), "stopped": ("■ Parado", "idle")}
LOCAL_ADDRS = ("127.0.0.1", "::1")
MAX_GLOBAL_FAILS = 30
PLACEHOLDERS = {
    "reserva": ("Reserva", "As propostas do algoritmo chegam na v0.4."),
}


def create_app(test_config=None):
    app = Flask(__name__)
    root = Path(__file__).resolve().parent.parent
    app.config.update(DATA_DIR=str(cfgmod.data_dir()), MARKET_FETCH=None, KEY_FILE=None, READER_FACTORY=None,
                      TESTNET_KEY_FILE=None, TELEGRAM_FILE=None, TRADER_FACTORY=None)
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
        dot = None
        if session.get("auth"):
            stopped = db.get(conn(), "emergency_stop") == "1"
            dot = {"alto": "alto", "atenção": "atencao", "info": "info"}.get(alerts.unseen_top(conn()))
        return {
            "csp_nonce": g.get("csp_nonce", ""),
            "states": STATES,
            "alert_dot": dot,
            "gl": explain, "rec": {"general": rec.GENERAL, "fields": rec.FIELDS, "disclaimer": rec.DISCLAIMER, "risk_items": rec.RISK_ITEMS},
            "csrf": session["csrf"],
            "cur": session.get("cur", "USDT"),
            "currencies": CURRENCIES,
            "stopped": stopped,
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
                               capital_label=capital_label)

    @app.route("/configuracao", methods=["GET", "POST"])
    def config():
        cfg = db.get_all(conn())
        if request.method == "POST":
            new, errors = parse_config(request.form)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("config.html", cfg=_merge(cfg, request.form),
                                       categories=EXCLUSION_CATEGORIES, risk_pairs=RISK_PAIRS)
            if request.form.get("step") == "save":
                db.set_many(conn(), new)
                changes = describe_changes(cfg, new)
                db.log(conn(), "Configuração guardada",
                       "; ".join(f"{a}: {b} → {c}" for a, b, c in changes))
                flash("Configuração guardada.", "ok")
                return redirect(url_for("config"))
            changes = describe_changes(cfg, new)
            if not changes:
                flash("Não mudaste nada.", "ok")
                return redirect(url_for("config"))
            return render_template("config_confirm.html", changes=changes, new=new)
        return render_template("config.html", cfg=cfg, categories=EXCLUSION_CATEGORIES, risk_pairs=RISK_PAIRS)

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
            flash("Tudo parado. O sistema está em modo seguro.", "ok")
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
    @app.get("/ai")
    def ai_page():
        pf_result = refresh_portfolio()
        pf = pf_result["snap"]
        if not pf:
            return render_template("ai.html", pf=None, result=pf_result)
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
                               summary_text=None if problems else text, problems=problems)

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
                    ok, err = (notify.SENDER or notify.send)(token, chat, "Malha: mensagem de teste. "
                                                             "Os alertas dos bots chegam aqui.")
                    if not ok:
                        flash(f"Não consegui enviar: {err}. Confirma o token e o id da conversa.", "error")
                    else:
                        if action == "save_telegram":
                            keystore.save(telegram_file(), token, chat)
                            db.log(conn(), "Telegram configurado (só envio)")
                        flash("Mensagem de teste enviada. O Telegram só envia alertas, não recebe comandos.", "ok")
            return redirect(url_for("bot_links"))
        tn, tg = keystore.load(testnet_file()), keystore.load(telegram_file())
        return render_template("bots_links.html", testnet=keystore.masked(tn[0]) if tn else None,
                               telegram=bool(tg))

    def bot_params():
        cfg = db.get_all(conn())
        return {"max_loss_trade_pct": float(cfg["max_loss_trade"]), "pause_drawdown_pct": float(cfg["pause_drawdown"])}

    def suggestions_cached(force=False):
        cache = app.extensions.setdefault("pair_cache", {"t": 0, "data": None, "error": None})
        if force or cache["data"] is None or time.time() - cache["t"] > 300:
            try:
                tickers = make_reader("", "").tickers_all()
                excl = [c for c in db.get(conn(), "exclusions").split(",") if c]
                cache.update(t=time.time(), data=pairs.suggest(tickers, excl or ("stables",)), error=None,
                             tickers=tickers)
            except Exception as exc:
                cache.update(t=time.time(), data=[], error=str(exc) if isinstance(exc, BinanceError) else "sem dados", tickers={})
        return cache

    def default_capital():
        snap = load_snapshot()
        return round(snap["total_usdt"] * 0.15, 2) if snap else 90.0

    def preview_grid(pair, capital, mode="sim"):
        """Monta a grelha com o preço e as regras reais do par. Levanta GridRefused se a guarda recusar."""
        if mode == "testnet":
            trader = make_trader()
            if not trader.has_keys:
                raise G.GridRefused("Faltam as chaves da Testnet. Guarda-as em Bots > Ligações.")
            rules = trader.symbol_rules(pair)
            if rules.get("status") != "TRADING":
                raise G.GridRefused(f"O par {pair} não está em negociação na Testnet.")
            if trader.free_balance("USDT") < capital:
                raise G.GridRefused("A conta da Testnet não tem USDT suficiente para este capital.")
            price = float(trader.ticker24(pair)["lastPrice"])
        else:
            reader = make_reader("", "")
            rules = reader.symbol_rules(pair)
            if rules.get("status") != "TRADING":
                raise G.GridRefused(f"O par {pair} não está em negociação normal na Binance.")
            price = float(reader.ticker24(pair)["lastPrice"])
        params = bot_params()
        return price, rules, params, G.build_grid(price, capital, rules, params)

    def testnet_pairs(cache, force=False):
        """Sugestões reais filtradas para os pares que existem na Testnet."""
        store = app.extensions.setdefault("testnet_syms", {"t": 0, "data": None})
        if force or store["data"] is None or time.time() - store["t"] > 300:
            store.update(t=time.time(), data=make_trader().trading_symbols())
        return pairs.for_testnet(cache["data"], store["data"])

    @app.get("/bots")
    def bots():
        rows = []
        for r in botstore.all_bots(conn()):
            eng = botstore.load_engine(conn(), r["id"])
            rows.append({"row": r, "stats": botstore.stats(eng, now_ms()), "eng": eng})
        return render_template("bots.html", rows=rows, runner=runner_state())

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
            twin = request.form.get("twin") == "1" and mode == "testnet"
            never_below = request.form.get("never_below_cost") == "1"
            try:
                capital = float(request.form.get("capital", "").replace(",", "."))
            except ValueError:
                capital = 0.0
            chosen = next((s for s in cache["data"] if s["pair"] == pair), None)
            if not chosen or capital <= 0:
                flash("Escolhe um dos pares sugeridos e indica um capital maior que zero.", "error")
                return redirect(url_for("bot_new", modo=mode))
            try:
                price, rules, params, g = preview_grid(pair, capital, mode)
            except (G.GridRefused, BinanceError, TraderError) as exc:
                flash(str(exc), "error")
                return redirect(url_for("bot_new", modo=mode))
            params = {**params, "never_sell_below_cost": never_below}   # opcional por bot, desligada por defeito
            if request.form.get("step") == "create":
                bid = botstore.create(conn(), pair, capital, rules, params, mode=mode)
                if twin:                                    # gémeo em simulação, com as mesmas velas da Testnet
                    botstore.create(conn(), pair, capital, rules, params, mode="sim", source="testnet", twin_of=bid)
                db.log(conn(), f"Bot criado ({'Testnet' if mode == 'testnet' else 'simulação'})",
                       f"{pair}, capital {capital:g} USDT{' + gémeo em simulação' if twin else ''}")
                flash("Bot criado e aprovado. O corredor arranca-o no próximo minuto. "
                      + ("Envia ordens só para a Testnet (dinheiro fictício)." if mode == "testnet"
                         else "É só simulação."), "ok")
                return redirect(url_for("bot_detail", bot_id=bid))
            return render_template("bot_preview.html", chosen=chosen, capital=capital, price=price, rules=rules, g=g,
                                   p={**G.DEFAULTS, **params}, cost=G.cost_pct(), mode=mode, twin=twin,
                                   never_below=never_below)
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
        if cmd in ("pause", "resume", "stop") and botstore.get(conn(), bot_id):
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
