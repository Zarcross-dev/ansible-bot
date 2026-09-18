# Ansible Control — bot Discord

Pilotage d'un parc Ansible depuis un salon Discord privé. Panneau à boutons,
commandes slash, sortie en direct et log complet joint à chaque exécution.

Version courante : **b0.0.1**

---

## Installation sur le LXC Ansible

Le bot doit tourner sur la machine qui a Ansible, ses clés SSH et le dossier
`~/infra`.

```bash
mkdir -p /opt/ansible-bot && cd /opt/ansible-bot
# y copier bot.py, config.json, requirements.txt, ansible-bot.service

apt install -y python3-venv
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Créer l'application Discord

1. Sur le portail développeur Discord, crée une application, puis un bot.
2. Copie le token.
3. Dans l'onglet OAuth2 → URL Generator, coche les scopes `bot` et
   `applications.commands`, puis les permissions `Send Messages`,
   `Embed Links`, `Attach Files` et `Read Message History`.
4. Ouvre l'URL générée pour inviter le bot sur ton serveur.

Le bot n'a pas besoin des intents privilégiés : il ne lit aucun message.

### Token

```bash
cp .env.example .env
nano .env          # coller le token
chmod 600 .env
```

### Lancer

```bash
cp ansible-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ansible-bot
journalctl -u ansible-bot -f
```

Puis, dans le salon Discord privé :

```
/setup
```

---

## Commandes

| Commande | Rôle |
|---|---|
| `/setup` | Publie le panneau à boutons dans le salon courant |
| `/update` | Recharge `config.json` et met à jour le message du panneau |
| `/run action: [limit:] [simulation:]` | Lance une action, éventuellement sur un seul hôte |
| `/status` | Job en cours et cinq dernières exécutions |
| `/cancel` | Interrompt le job en cours |
| `/logs [index:]` | Renvoie le log complet d'une exécution (0 = la plus récente) |
| `/reload` | Recharge la config sans toucher au panneau |
| `/version` | Version du bot, de discord.py et d'Ansible |
| `/exec pattern: command:` | Commande ad-hoc libre (désactivée par défaut) |

Toutes les commandes sont réservées au rôle admin défini dans la config, et
limitées au salon configuré (sauf `/setup`, `/update` et `/reload`).

---

## Ajouter une action

Ajoute un objet dans `actions` de `config.json`, puis lance `/update` dans
Discord. Aucun redémarrage du bot n'est nécessaire.

### Action de type playbook

```json
{
  "id": "backup_configs",
  "label": "Sauvegarder les configs",
  "emoji": "💾",
  "style": "success",
  "row": 3,
  "description": "Rapatrie les fichiers de conf sur le LXC.",
  "type": "playbook",
  "playbook": "backup.yml",
  "extra_vars": { "dest": "/root/backups" },
  "confirm": false,
  "timeout": 1800,
  "button": true
}
```

### Action de type ad-hoc

```json
{
  "id": "free",
  "label": "Mémoire",
  "emoji": "🧠",
  "style": "secondary",
  "row": 0,
  "description": "Mémoire libre sur chaque hôte.",
  "type": "adhoc",
  "pattern": "all",
  "module": "shell",
  "args": "free -h",
  "become": true,
  "timeout": 120,
  "button": true
}
```

### Champs disponibles

| Champ | Effet |
|---|---|
| `id` | Identifiant unique, sert au `custom_id` du bouton |
| `label`, `emoji`, `description` | Affichage |
| `style` | `primary`, `secondary`, `success`, `danger` |
| `row` | Ligne du bouton, 0 à 4, **5 boutons maximum par ligne** |
| `button` | `false` pour une action accessible seulement via `/run` |
| `confirm` | `true` ajoute une double validation éphémère |
| `confirm_text` | Message d'avertissement de la confirmation |
| `type` | `playbook` ou `adhoc` |
| `playbook` | Nom du fichier, relatif à `ansible.workdir` |
| `extra_vars` | Objet transformé en `-e cle=valeur` |
| `tags` | Liste transformée en `--tags` |
| `pattern`, `module`, `args`, `become` | Pour les actions ad-hoc |
| `limit` | Valeur par défaut de `--limit` |
| `check` | `true` force le mode `--check` |
| `extra_args` | Liste d'arguments bruts ajoutés à la fin |
| `timeout` | Durée maximale en secondes |

Attention : les arguments sont passés à Ansible, qui les interprète en Jinja2.
N'utilise pas `{{` ni `}}` dans un champ `args`, sauf pour du templating Ansible
volontaire.

---

## Sécurité

Trois barrières : le bot ne répond que sur la guilde configurée, uniquement dans
le salon configuré, et uniquement aux porteurs du rôle admin.

Ce bot donne un accès root à tout le parc depuis Discord. Garde donc le salon
privé, restreint aux deux admins, et n'active `allow_exec` qu'en connaissance de
cause : cette commande permet d'exécuter n'importe quoi en root sur les hôtes.
Le token Discord vaut un accès à ton infrastructure, protège `.env` comme une
clé SSH.

Les commandes sont exécutées via `execve` avec une liste d'arguments, jamais à
travers un shell côté bot, ce qui évite toute injection depuis les paramètres
des commandes slash.

---

## Fonctionnement

Un seul job tourne à la fois : un verrou empêche deux mises à jour simultanées.
Le message est édité au maximum toutes les deux secondes avec les dernières
lignes de sortie, ce qui reste sous les limites de l'API Discord.

Chaque exécution écrit un log dans `logs/`, joint au message final et purgé
après `log_retention_days` jours.

Le `PLAY RECAP` d'Ansible est parsé pour afficher un résumé par hôte avec un
code couleur.

À l'annulation, le bot envoie `SIGTERM` au groupe de processus, puis `SIGKILL`
après cinq secondes.

---

## Journal des versions

### b0.0.1
Première version : panneau à boutons piloté par config, `/setup` et `/update`,
actions playbook et ad-hoc, confirmation des actions destructrices, sortie en
direct, logs joints, contrôle d'accès par rôle, salon et guilde.

### Pistes pour la suite
Snapshots Proxmox avant mise à jour, exécutions planifiées avec rapport
automatique, notification en cas d'échec, historique persistant entre
redémarrages, playbook de contrôle de santé.
