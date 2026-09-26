"""Ponto de entrada para um servidor de produção.

  waitress-serve --listen=127.0.0.1:5000 --call app:create_app      (usado nos serviços do Pi)
  gunicorn wsgi:app
"""
from app import create_app

app = create_app()
