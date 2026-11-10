class TerraPiloteError(Exception):
    """Erreur attendue, affichée à l'utilisateur sans trace Python."""


class CommandError(TerraPiloteError):
    def __init__(self, argv, returncode, stderr=""):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr or ""
        detail = f": {self.stderr.strip()}" if self.stderr.strip() else ""
        super().__init__(f"La commande `{' '.join(self.argv)}` a échoué (code {returncode}){detail}")
