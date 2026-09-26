"""Arranca o painel em modo de desenvolvimento (PC). No Pi usa o serviço com waitress (deploy/). Só aceita ligações deste computador.

No Raspberry Pi: sudo bash deploy/install_pi.sh (servidor waitress, só em 127.0.0.1; acesso por túnel SSH ou VPN).
"""
import os

from app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(host=os.environ.get("BOTS_HOST", "127.0.0.1"),
            port=int(os.environ.get("BOTS_PORT", "5000")))
