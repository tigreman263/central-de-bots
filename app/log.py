"""Registo (logging) do corredor: para stdout, onde o systemd/journald o recolhe e roda. Nunca regista chaves."""
import logging
import sys

log = logging.getLogger("bots")


def setup(level=logging.INFO):
    if log.handlers:
        return log
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = False
    return log
