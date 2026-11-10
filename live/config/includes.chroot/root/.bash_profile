# Au démarrage de la clé (tty1) : interface graphique, sinon menu texte.
# Paramètre de démarrage « clonix.text » : menu texte directement.
[ -f ~/.bashrc ] && . ~/.bashrc
if [ "$(tty)" = /dev/tty1 ] && [ -z "${CLONIX_STARTED:-}" ]; then
    export CLONIX_STARTED=1
    if ! grep -qw clonix.text /proc/cmdline; then
        clonix-kiosk && exit 0
        echo "Affichage graphique indisponible : passage au menu texte."
        sleep 2
    fi
    clonix menu
fi
