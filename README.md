# AgroManager — Déploiement Docker / Portainer

Application de gestion agricole complète pour l'Exploitation Masson.
Stack : **Node.js + SQLite** (backend) + **Nginx** (frontend)

---

## 📁 Structure du projet

```
agromanager/
├── docker-compose.yml          ← Stack principale
├── backend/
│   ├── Dockerfile
│   ├── package.json
│   └── server.js               ← API Express + SQLite
└── frontend/
    ├── Dockerfile
    ├── nginx.conf              ← Proxy API + serve HTML
    └── index.html              ← Application complète
```

---

## 🚀 Déploiement rapide (ligne de commande)

```bash
# 1. Cloner / copier le dossier sur ton serveur
scp -r agromanager/ user@VOTRE_IP:/opt/agromanager

# 2. Se placer dans le dossier
cd /opt/agromanager

# 3. Construire et lancer
docker compose up -d --build

# 4. Vérifier que tout tourne
docker compose ps
docker compose logs -f
```

L'application est accessible sur **http://VOTRE_IP:8081**

---

## 🐳 Déploiement via Portainer

### Méthode 1 — Stack depuis fichier (recommandée)

1. Dans Portainer → **Stacks** → **Add stack**
2. Nom : `agromanager`
3. Choisir **Upload** → uploader le fichier `docker-compose.yml`
4. Cliquer **Deploy the stack**

> ⚠️ Portainer doit avoir accès aux fichiers `backend/` et `frontend/`
> → Copier d'abord le dossier complet sur le serveur, puis pointer vers le chemin

### Méthode 2 — Stack depuis Git (si tu mets le projet sur Gitea/GitHub)

1. Portainer → **Stacks** → **Add stack** → **Repository**
2. URL : `http://VOTRE_GITEA/user/agromanager`
3. Compose path : `docker-compose.yml`

### Méthode 3 — Stack en éditant le YAML directement

1. Portainer → **Stacks** → **Add stack** → **Web editor**
2. Coller le contenu du `docker-compose.yml`

> ⚠️ Avec cette méthode, les images doivent être **pré-buildées** et poussées
> vers un registre (ex: ton registre Portainer local ou Docker Hub)

---

## 🔧 Configuration du port

Par défaut : port **8081**

Pour changer (ex: port 80) :
```yaml
# docker-compose.yml
ports:
  - "80:80"    # ← modifier ici
```

Ou depuis Portainer → **Stacks** → modifier le YAML → **Update the stack**

---

## 💾 Base de données SQLite

La base est stockée dans un **volume Docker nommé** `agromanager-data`.
Elle persiste même si les containers sont supprimés.

```bash
# Voir la base
docker exec agromanager-backend ls -lh /data/

# Backup manuel
docker run --rm \
  -v agromanager-data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/agromanager_backup_$(date +%Y%m%d).tar.gz /data

# Restaurer un backup
docker run --rm \
  -v agromanager-data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/agromanager_backup_YYYYMMDD.tar.gz -C /
```

### Backup JSON depuis l'application

Dans l'app → **Rapports** → bouton **💾 Backup JSON**
→ Télécharge un fichier JSON complet de toutes tes données

---

## 🔄 Mise à jour

```bash
cd /opt/agromanager

# Mettre à jour le code
git pull   # ou re-copier les fichiers modifiés

# Reconstruire et relancer
docker compose up -d --build

# Les données sont préservées (volume Docker)
```

Depuis Portainer : **Stacks** → `agromanager` → **Pull and redeploy**

---

## 🌐 API REST disponible

| Méthode | URL | Description |
|---------|-----|-------------|
| GET | `/api/parcelles` | Liste des parcelles |
| POST | `/api/parcelles` | Ajouter une parcelle |
| PUT | `/api/parcelles/:id` | Modifier une parcelle |
| DELETE | `/api/parcelles/:id` | Supprimer une parcelle |
| GET | `/api/interventions` | Liste des interventions |
| POST | `/api/interventions` | Ajouter une intervention |
| PUT | `/api/interventions/:id` | Modifier |
| DELETE | `/api/interventions/:id` | Supprimer |
| GET | `/api/stocks` | Inventaire stocks |
| POST | `/api/stocks` | Nouveau produit |
| PUT | `/api/stocks/:id` | Modifier produit |
| DELETE | `/api/stocks/:id` | Supprimer produit |
| POST | `/api/mouvements` | Entrée/sortie de stock |
| DELETE | `/api/mouvements/:id` | Supprimer mouvement |
| GET | `/api/materiels` | Parc matériel |
| POST | `/api/materiels` | Ajouter matériel |
| PUT | `/api/materiels/:id` | Modifier |
| DELETE | `/api/materiels/:id` | Supprimer |
| GET | `/api/travailleurs` | Équipe |
| POST | `/api/travailleurs` | Ajouter travailleur |
| PUT | `/api/travailleurs/:id` | Modifier |
| DELETE | `/api/travailleurs/:id` | Supprimer |
| GET | `/api/backup` | Export JSON complet |
| GET | `/api/health` | Health check |

---

## 🔒 Accès réseau

- Le **backend** (port 3001) n'est **pas exposé** à l'extérieur
- Seul le **frontend** (port 8081) est accessible
- Le frontend proxifie les requêtes `/api-backend/` vers le backend via le réseau interne Docker

Pour accéder depuis l'extérieur du réseau local → configurer un reverse proxy
(Nginx Proxy Manager, Traefik, Caddy) devant le port 8081.

---

## 🐛 Dépannage

```bash
# Logs backend
docker logs agromanager-backend -f

# Logs frontend
docker logs agromanager-frontend -f

# Vérifier l'API directement (depuis le serveur)
curl http://localhost:3001/api/health

# Redémarrer un container
docker restart agromanager-backend
```
