"""Uma só instância do corredor: dois corredores ao mesmo tempo enviariam ordens duplicadas."""
import os


def acquire(path):
    """Devolve o ficheiro aberto e bloqueado (guarda-o vivo enquanto o processo correr), ou None se já há outro."""
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh
