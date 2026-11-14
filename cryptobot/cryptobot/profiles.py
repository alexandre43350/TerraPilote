"""Profils de risque prêts à l'emploi.

Choisis en walk-forward sur 2018-2024, puis vérifiés UNE fois sur 2025-2026 (période réservée,
marché baissier : Buy & Hold −40 %). Chiffres out-of-sample, 4h, BTC+ETH, frais 0,1 % + slippage 0,05 %.

| Profil     | 2020-26 CAGR | Sharpe | Perte max | 12 mois perdants | Pire 12 mois | 2025-26 (BH −40 %) |
|------------|-------------:|-------:|----------:|-----------------:|-------------:|-------------------:|
| prudent    |       +5,5 % |   0,75 |    −8,8 % |             17 % |         −8 % |             −0,4 % |
| equilibre  |      +28,2 % |   1,13 |   −23,6 % |             18 % |        −23 % |             +9,1 % |
| dynamique  |      +43,1 % |   1,30 |   −35,8 % |             19 % |        −33 % |             +7,8 % |
| Buy & Hold |      +42,5 % |   0,86 |   −79,6 % |             40 % |        −77 % |              −40 % |
"""

PROFILES = {
    "prudent": {
        "description": "Priorité : ne pas perdre. Filtre de marché BTC, faible exposition, stop + attente d'un nouveau signal.",
        "strategy": "rotation",
        "params": {},
        "risk": {"target_vol": 0.30, "stop_atr": 3.0, "reentry": "fresh"},
    },
    "equilibre": {
        "description": "Compromis rendement / risque. Ensemble de 4 modèles, volatilité visée 60 %, stop 3 ATR.",
        "strategy": "ensemble",
        "params": {},
        "risk": {"target_vol": 0.60, "stop_atr": 3.0, "reentry": "cooldown"},
    },
    "dynamique": {
        "description": "Rendement proche du Buy & Hold avec ~2× moins de perte max. Ensemble, volatilité 100 %, sans stop.",
        "strategy": "ensemble",
        "params": {},
        "risk": {"target_vol": 1.00, "stop_atr": None, "reentry": "cooldown"},
    },
}
