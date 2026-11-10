# Au démarrage de la clé (tty1) : interface graphique, sinon menu texte.
# Paramètre de démarrage « terrapilote.text » : menu texte directement.
[ -f ~/.bashrc ] && . ~/.bashrc
if [ "$(tty)" = /dev/tty1 ] && [ -z "${TP_STARTED:-}" ]; then
    export TP_STARTED=1
    if ! grep -qw terrapilote.text /proc/cmdline; then
        terrapilote-kiosk && exit 0
        echo "Affichage graphique indisponible : passage au menu texte."
        sleep 2
    fi
    terrapilote menu
fi
