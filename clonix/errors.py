class ClonixError(Exception):
    """Erreur attendue, affichée à l'utilisateur sans trace Python."""


class Cancelled(ClonixError):
    """Opération annulée par l'utilisateur."""


class CommandError(ClonixError):
    def __init__(self, argv, returncode, stderr=""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr or ""
        detail = f": {self.stderr.strip()}" if self.stderr.strip() else ""
        super().__init__(f"La commande `{' '.join(self.argv)}` a échoué (code {returncode}){detail}")
