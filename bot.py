#!/usr/bin/env python3
"""
Ansible Control — bot Discord de pilotage d'un parc Ansible.

Tout est piloté par config.json : ajouter une action au fichier puis lancer
/update suffit à faire apparaître un nouveau bouton dans le panneau.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ANSIBLE_BOT_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.environ.get("ANSIBLE_BOT_STATE", BASE_DIR / "state.json"))

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
MAX_BLOCK = 3400          # taille max du bloc de log dans un embed
LIVE_LINES = 18           # nombre de lignes affichées en direct
EDIT_INTERVAL = 2.0       # secondes minimum entre deux éditions du message
INTERACTION_TTL = 14 * 60 # durée de vie utile du token d'interaction (15 min côté Discord)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ansible-bot")

STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}


# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #
class Config:
    """Wrapper autour de config.json, rechargeable à chaud."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self.path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        self._validate(data)
        self.data = data
        log.info("Configuration chargée : %d actions, version %s",
                 len(self.actions), self.version)

    def _validate(self, data: dict[str, Any]) -> None:
        for key in ("version", "discord", "ansible", "actions"):
            if key not in data:
                raise ValueError(f"config.json : clé « {key} » manquante")
        seen: set[str] = set()
        for action in data["actions"]:
            for key in ("id", "label", "type"):
                if key not in action:
                    raise ValueError(f"action {action!r} : clé « {key} » manquante")
            if action["id"] in seen:
                raise ValueError(f"action « {action['id']} » définie deux fois")
            seen.add(action["id"])
            if action["type"] == "playbook" and "playbook" not in action:
                raise ValueError(f"action « {action['id']} » : « playbook » manquant")
            if action["type"] == "adhoc" and "module" not in action:
                raise ValueError(f"action « {action['id']} » : « module » manquant")
            if action["type"] not in ("playbook", "adhoc"):
                raise ValueError(f"action « {action['id']} » : type inconnu")

    # Raccourcis ------------------------------------------------------------ #
    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def discord(self) -> dict[str, Any]:
        return self.data["discord"]

    @property
    def ansible(self) -> dict[str, Any]:
        return self.data["ansible"]

    @property
    def panel(self) -> dict[str, Any]:
        return self.data.get("panel", {})

    @property
    def actions(self) -> list[dict[str, Any]]:
        return self.data["actions"]

    def action(self, action_id: str) -> dict[str, Any] | None:
        return next((a for a in self.actions if a["id"] == action_id), None)

    @property
    def workdir(self) -> Path:
        return Path(self.ansible.get("workdir", ".")).expanduser()

    @property
    def log_dir(self) -> Path:
        d = Path(self.ansible.get("log_dir", "logs"))
        return d if d.is_absolute() else BASE_DIR / d


# --------------------------------------------------------------------------- #
#  État persistant (id du message du panneau)
# --------------------------------------------------------------------------- #
def read_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json illisible, réinitialisé")
    return {}


def write_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
#  Construction des commandes Ansible
# --------------------------------------------------------------------------- #
def build_argv(cfg: Config, action: dict[str, Any],
               limit: str | None = None, check: bool | None = None) -> list[str]:
    """Traduit une action de config.json en argv (jamais de shell côté bot)."""
    a = cfg.ansible
    use_check = action.get("check", False) if check is None else check

    if action["type"] == "playbook":
        argv = [a.get("playbook_cmd", "ansible-playbook"), action["playbook"]]
        for name, value in (action.get("extra_vars") or {}).items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"      # et non True/False
            elif isinstance(value, (dict, list)):
                rendered = json.dumps(value)
            else:
                rendered = str(value)
            argv += ["-e", f"{name}={rendered}"]
        if action.get("tags"):
            argv += ["--tags", ",".join(action["tags"])]
    else:  # adhoc
        argv = [a.get("adhoc_cmd", "ansible"), action.get("pattern", "all"),
                "-m", action["module"]]
        if action.get("args"):
            argv += ["-a", action["args"]]
        if action.get("become", False):
            argv.append("-b")

    target = limit or action.get("limit")
    if target:
        argv += ["--limit", target]
    if use_check:
        argv.append("--check")
    argv += action.get("extra_args", [])
    return argv


def build_env(cfg: Config) -> dict[str, str]:
    env = os.environ.copy()
    env.update({k: str(v) for k, v in (cfg.ansible.get("env") or {}).items()})
    return env


# --------------------------------------------------------------------------- #
#  Exécution d'un job
# --------------------------------------------------------------------------- #
class Job:
    """Une exécution Ansible en cours, avec sa sortie et son message Discord."""

    def __init__(self, cfg: Config, action: dict[str, Any], argv: list[str],
                 user: discord.abc.User):
        self.cfg = cfg
        self.action = action
        self.argv = argv
        self.user = user
        self.started_at = time.monotonic()
        self.started_wall = datetime.now(timezone.utc)
        self.lines: list[str] = []
        self.tail: deque[str] = deque(maxlen=LIVE_LINES)
        self.proc: asyncio.subprocess.Process | None = None
        self.returncode: int | None = None
        self.cancelled = False
        self.timed_out = False
        self.log_path: Path | None = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def pretty_cmd(self) -> str:
        return " ".join(shlex.quote(p) for p in self.argv)

    # ---------------------------------------------------------------- run -- #
    async def run(self, on_update) -> None:
        timeout = self.action.get("timeout", self.cfg.ansible.get("default_timeout", 3600))
        workdir = self.cfg.workdir

        if not workdir.is_dir():
            self.lines.append(f"[bot] Répertoire de travail introuvable : {workdir}")
            self.returncode = 127
            return

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(workdir),
                env=build_env(self.cfg),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,      # groupe de process propre pour l'annulation
            )
        except FileNotFoundError:
            self.lines.append(f"[bot] Commande introuvable : {self.argv[0]}")
            self.returncode = 127
            return

        pump = asyncio.create_task(self._pump(on_update))
        try:
            await asyncio.wait_for(pump, timeout=timeout)
        except asyncio.TimeoutError:
            self.timed_out = True
            self.lines.append(f"[bot] Délai de {timeout}s dépassé, arrêt du processus.")
            await self.kill()
            pump.cancel()
        finally:
            if self.proc.returncode is None:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    await self.kill(force=True)
            self.returncode = self.proc.returncode

        self._write_log()

    async def _pump(self, on_update) -> None:
        assert self.proc and self.proc.stdout
        last_edit = 0.0
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                break
            line = ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
            self.lines.append(line)
            if line.strip():
                self.tail.append(line)
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL:
                last_edit = now
                await on_update(self)
        await self.proc.wait()

    async def kill(self, force: bool = False) -> None:
        if not self.proc or self.proc.returncode is not None:
            return
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

    async def cancel(self) -> None:
        self.cancelled = True
        self.lines.append("[bot] Annulation demandée.")
        await self.kill()
        await asyncio.sleep(5)
        await self.kill(force=True)

    # --------------------------------------------------------------- logs -- #
    def _write_log(self) -> None:
        d = self.cfg.log_dir
        d.mkdir(parents=True, exist_ok=True)
        stamp = self.started_wall.strftime("%Y%m%d-%H%M%S")
        self.log_path = d / f"{stamp}_{self.action['id']}.log"
        header = [
            f"# action    : {self.action['id']} ({self.action.get('label', '')})",
            f"# demandé par: {self.user} ({self.user.id})",
            f"# commande   : {self.pretty_cmd}",
            f"# répertoire : {self.cfg.workdir}",
            f"# début      : {self.started_wall.isoformat()}",
            f"# durée      : {self.elapsed:.1f}s",
            f"# code retour: {self.returncode}",
            "",
        ]
        self.log_path.write_text("\n".join(header + self.lines), encoding="utf-8")
        self._prune_logs()

    def _prune_logs(self) -> None:
        days = self.cfg.ansible.get("log_retention_days", 30)
        if not days:
            return
        cutoff = time.time() - days * 86400
        for f in self.cfg.log_dir.glob("*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    # -------------------------------------------------------------- embed -- #
    def status(self) -> tuple[str, discord.Colour]:
        if self.returncode is None:
            return "⏳ En cours", discord.Colour.blurple()
        if self.cancelled:
            return "🛑 Annulé", discord.Colour.orange()
        if self.timed_out:
            return "⌛ Délai dépassé", discord.Colour.orange()
        if self.returncode == 0:
            return "✅ Terminé", discord.Colour.green()
        return f"❌ Échec (code {self.returncode})", discord.Colour.red()

    def embed(self) -> discord.Embed:
        state, colour = self.status()
        action = self.action
        emb = discord.Embed(
            title=f"{action.get('emoji', '⚙️')} {action.get('label', action['id'])}",
            colour=colour,
            timestamp=datetime.now(timezone.utc),
        )
        emb.add_field(name="Statut", value=state, inline=True)
        emb.add_field(name="Durée", value=f"{self.elapsed:.0f}s", inline=True)
        emb.add_field(name="Demandé par", value=self.user.mention, inline=True)
        emb.add_field(name="Commande", value=f"`{self.pretty_cmd[:1000]}`", inline=False)

        body = "\n".join(self.tail) if self.tail else "(pas encore de sortie)"
        if len(body) > MAX_BLOCK:
            body = "…\n" + body[-MAX_BLOCK:]
        emb.add_field(name="Sortie", value=f"```\n{body}\n```", inline=False)

        if self.returncode is not None:
            emb.add_field(name="Récapitulatif", value=self._recap(), inline=False)
        emb.set_footer(text=f"{self.cfg.panel.get('footer_note', 'AnsibleBot')} • {self.cfg.version}")
        return emb

    def _recap(self) -> str:
        """Extrait le PLAY RECAP de la sortie, sinon les dernières lignes."""
        try:
            idx = max(i for i, l in enumerate(self.lines) if "PLAY RECAP" in l)
        except ValueError:
            return "—"
        recap = [l for l in self.lines[idx + 1:] if l.strip()]
        out: list[str] = []
        for line in recap[:12]:
            m = re.match(r"^(\S+)\s*:\s*(.*)$", line)
            if not m:
                continue
            host, rest = m.group(1), m.group(2)
            bad = re.search(r"failed=([1-9]\d*)", rest) or re.search(r"unreachable=([1-9]\d*)", rest)
            changed = re.search(r"changed=([1-9]\d*)", rest)
            icon = "❌" if bad else ("🔧" if changed else "✅")
            out.append(f"{icon} `{host}` — {rest.strip()}")
        return "\n".join(out)[:1024] or "—"


# --------------------------------------------------------------------------- #
#  Registre des jobs (un seul à la fois)
# --------------------------------------------------------------------------- #
class JobManager:
    def __init__(self):
        self.current: Job | None = None
        self.history: deque[Job] = deque(maxlen=20)
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.current is not None

    async def run(self, job: Job, on_update) -> Job:
        async with self._lock:
            self.current = job
            try:
                await job.run(on_update)
            finally:
                self.current = None
                self.history.appendleft(job)
        return job


# --------------------------------------------------------------------------- #
#  Contrôle d'accès
# --------------------------------------------------------------------------- #
class Denied(app_commands.CheckFailure):
    pass


def check_access(cfg: Config, interaction: discord.Interaction,
                 enforce_channel: bool = True) -> None:
    d = cfg.discord
    if interaction.guild_id != d["guild_id"]:
        raise Denied("Ce bot n'est utilisable que sur le serveur configuré.")
    if enforce_channel and d.get("restrict_to_channel", True):
        if interaction.channel_id != d["channel_id"]:
            raise Denied(f"À utiliser dans <#{d['channel_id']}> uniquement.")
    member = interaction.user
    if not isinstance(member, discord.Member):
        raise Denied("Impossible de vérifier tes rôles.")
    if d.get("allow_server_administrators", True) and member.guild_permissions.administrator:
        return
    if any(r.id == d["admin_role_id"] for r in member.roles):
        return
    raise Denied(f"Réservé aux porteurs du rôle <@&{d['admin_role_id']}>.")


# --------------------------------------------------------------------------- #
#  Composants d'interface
# --------------------------------------------------------------------------- #
class ConfirmView(discord.ui.View):
    """Double validation pour les actions marquées confirm."""

    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Cette confirmation ne t'est pas destinée.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirmer", style=discord.ButtonStyle.danger, emoji="✔️")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Annuler", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


class ActionButton(discord.ui.Button):
    """Bouton du panneau. Ne retient que l'id : l'action est relue à chaud."""

    def __init__(self, bot: "AnsibleBot", action: dict[str, Any]):
        super().__init__(
            label=action.get("label", action["id"])[:80],
            emoji=action.get("emoji"),
            style=STYLES.get(action.get("style", "secondary"), discord.ButtonStyle.secondary),
            row=action.get("row", 0),
            custom_id=f"ansible:run:{action['id']}",
        )
        self.bot = bot
        self.action_id = action["id"]

    async def callback(self, interaction: discord.Interaction):
        await self.bot.trigger(interaction, self.action_id)


class PanelView(discord.ui.View):
    """Vue persistante, reconstruite depuis la config à chaque démarrage."""

    def __init__(self, bot: "AnsibleBot"):
        super().__init__(timeout=None)
        placed = 0
        for action in bot.cfg.actions:
            if not action.get("button", True):
                continue
            if placed >= 25:
                log.warning("Plus de 25 boutons : « %s » ignoré", action["id"])
                continue
            self.add_item(ActionButton(bot, action))
            placed += 1


def panel_embed(cfg: Config) -> discord.Embed:
    p = cfg.panel
    emb = discord.Embed(
        title=p.get("title", "Ansible"),
        description=p.get("description", ""),
        colour=discord.Colour(int(p.get("color", "5865F2"), 16)),
    )
    if p.get("show_action_list", True):
        groups: dict[int, list[str]] = {}
        for a in cfg.actions:
            if not a.get("button", True):
                continue
            mark = " ⚠️" if a.get("confirm") else ""
            desc = a.get("description", "")
            groups.setdefault(a.get("row", 0), []).append(
                f"{a.get('emoji', '•')} **{a.get('label', a['id'])}**{mark} — {desc}")
        for row in sorted(groups):
            emb.add_field(name="\u200b", value="\n".join(groups[row])[:1024], inline=False)
    emb.add_field(
        name="\u200b",
        value="Les actions marquées ⚠️ demandent une confirmation. "
              "`/run`, `/status`, `/cancel` et `/logs` sont aussi disponibles.\n"
              "Le suivi d'exécution et les logs ne sont visibles que par toi.",
        inline=False,
    )
    emb.set_footer(text=f"{p.get('footer_note', 'AnsibleBot')} • {cfg.version}")
    emb.timestamp = datetime.now(timezone.utc)
    return emb


# --------------------------------------------------------------------------- #
#  Restitution éphémère d'un job
# --------------------------------------------------------------------------- #
class JobReporter:
    """Affiche l'avancement d'un job dans la réponse éphémère de l'interaction.

    Rien n'est jamais publié en clair dans le salon : le suivi vit dans la
    réponse éphémère, et si le token d'interaction expire (Discord le limite à
    15 minutes) le résultat final part en message privé.
    """

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.expired = False

    @property
    def alive(self) -> bool:
        age = (datetime.now(timezone.utc) - self.interaction.created_at).total_seconds()
        return not self.expired and age < INTERACTION_TTL

    @staticmethod
    def _files(log_path: Path | None) -> list[discord.File]:
        if log_path and log_path.exists() and log_path.stat().st_size > 0:
            return [discord.File(log_path, filename=log_path.name)]
        return []

    async def update(self, embed: discord.Embed) -> bool:
        """Rafraîchit la vue en direct. Silencieux si le token n'est plus valide."""
        if not self.alive:
            return False
        try:
            await self.interaction.edit_original_response(
                content=None, embed=embed, view=None)
            return True
        except discord.HTTPException as exc:
            log.debug("Suivi éphémère interrompu : %s", exc)
            self.expired = True
            return False

    async def finish(self, embed: discord.Embed, log_path: Path | None) -> None:
        """Rendu final, log complet joint, toujours en privé."""
        if self.alive:
            try:
                await self.interaction.edit_original_response(
                    content=None, embed=embed, view=None, attachments=self._files(log_path))
                return
            except discord.HTTPException as exc:
                log.debug("Pièce jointe éphémère refusée : %s", exc)
            try:
                await self.interaction.edit_original_response(
                    content=None, embed=embed, view=None)
                files = self._files(log_path)
                if files:
                    await self.interaction.followup.send(files=files, ephemeral=True)
                return
            except discord.HTTPException as exc:
                log.debug("Réponse éphémère inaccessible : %s", exc)
                self.expired = True
        await self._dm(embed, log_path)

    async def _dm(self, embed: discord.Embed, log_path: Path | None) -> None:
        try:
            await self.interaction.user.send(
                content="Le suivi éphémère a expiré, voici le résultat :",
                embed=embed, files=self._files(log_path))
        except discord.HTTPException as exc:
            log.warning("Résultat non remis à %s (DM fermés ?) : %s",
                        self.interaction.user, exc)


# --------------------------------------------------------------------------- #
#  Le bot
# --------------------------------------------------------------------------- #
class AnsibleBot(discord.Client):
    def __init__(self, cfg: Config):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.tree = app_commands.CommandTree(self)
        self.jobs = JobManager()
        self.state = read_state()
        self.guild_obj = discord.Object(id=cfg.discord["guild_id"])

    async def setup_hook(self) -> None:
        self.add_view(PanelView(self))
        register_commands(self)
        self.tree.copy_global_to(guild=self.guild_obj)
        await self.tree.sync(guild=self.guild_obj)
        log.info("Commandes synchronisées sur la guilde %s", self.cfg.discord["guild_id"])

    async def on_ready(self) -> None:
        log.info("Connecté en tant que %s (%s)", self.user, self.user.id)
        #await self.change_presence(activity=discord.Game(name=f"Ansible {self.cfg.version}"))

    # ------------------------------------------------------- lancer un job -- #
    async def trigger(self, interaction: discord.Interaction, action_id: str,
                      limit: str | None = None, check: bool | None = None) -> None:
        try:
            check_access(self.cfg, interaction)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return

        action = self.cfg.action(action_id)
        if action is None:
            await interaction.response.send_message(
                f"Action « {action_id} » inconnue. Lance `/reload` si tu viens de modifier la config.",
                ephemeral=True)
            return

        if self.jobs.busy:
            cur = self.jobs.current
            await interaction.response.send_message(
                f"⏳ Une action est déjà en cours : **{cur.action.get('label')}** "
                f"({cur.elapsed:.0f}s). Utilise `/cancel` pour l'interrompre.",
                ephemeral=True)
            return

        argv = build_argv(self.cfg, action, limit=limit, check=check)

        # Confirmation éphémère si nécessaire
        if action.get("confirm"):
            view = ConfirmView(interaction.user.id)
            warn = action.get("confirm_text", "Cette action modifie l'état des serveurs.")
            emb = discord.Embed(
                title=f"Confirmer : {action.get('label')}",
                description=f"{warn}\n\n```\n{' '.join(shlex.quote(p) for p in argv)}\n```",
                colour=discord.Colour.orange(),
            )
            await interaction.response.send_message(embed=emb, view=view, ephemeral=True)
            await view.wait()
            if view.value is not True:
                await interaction.edit_original_response(
                    content="Annulé." if view.value is False else "Confirmation expirée.",
                    embed=None, view=None)
                return
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

        starting = discord.Embed(title="Démarrage…", colour=discord.Colour.blurple())
        await interaction.edit_original_response(content=None, embed=starting, view=None)

        job = Job(self.cfg, action, argv, interaction.user)
        reporter = JobReporter(interaction)
        log.info("[%s] %s → %s", interaction.user, action_id, job.pretty_cmd)

        async def on_update(j: Job) -> None:
            await reporter.update(j.embed())

        await reporter.update(job.embed())
        await self.jobs.run(job, on_update)
        await reporter.finish(job.embed(), job.log_path)
        log.info("[%s] terminé, code %s en %.1fs", action_id, job.returncode, job.elapsed)


# --------------------------------------------------------------------------- #
#  Commandes slash
# --------------------------------------------------------------------------- #
def register_commands(bot: AnsibleBot) -> None:
    tree = bot.tree

    async def action_autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [
            app_commands.Choice(name=f"{a.get('emoji','')} {a.get('label', a['id'])}"[:100],
                                value=a["id"])
            for a in bot.cfg.actions
            if current in a["id"].lower() or current in a.get("label", "").lower()
        ][:25]

    # ---------------------------------------------------------------- setup #
    @tree.command(description="Publier le panneau de contrôle Ansible dans ce salon")
    async def setup(interaction: discord.Interaction):
        try:
            check_access(bot.cfg, interaction, enforce_channel=False)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.channel.send(embed=panel_embed(bot.cfg), view=PanelView(bot))
        bot.state["panel"] = {"channel_id": msg.channel.id, "message_id": msg.id}
        write_state(bot.state)
        await interaction.followup.send(
            f"Panneau publié. `/update` le mettra à jour après modification de `config.json`.",
            ephemeral=True)

    # --------------------------------------------------------------- update #
    @tree.command(description="Recharger config.json et mettre à jour le message du panneau")
    async def update(interaction: discord.Interaction):
        try:
            check_access(bot.cfg, interaction, enforce_channel=False)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            bot.cfg.load()
        except Exception as exc:
            await interaction.followup.send(f"❌ config.json invalide : `{exc}`", ephemeral=True)
            return

        panel = bot.state.get("panel")
        if not panel:
            await interaction.followup.send(
                "Aucun panneau enregistré, lance `/setup` d'abord.", ephemeral=True)
            return
        try:
            channel = bot.get_channel(panel["channel_id"]) or \
                await bot.fetch_channel(panel["channel_id"])
            msg = await channel.fetch_message(panel["message_id"])
            await msg.edit(embed=panel_embed(bot.cfg), view=PanelView(bot))
        except discord.NotFound:
            await interaction.followup.send(
                "Message du panneau introuvable (supprimé ?). Relance `/setup`.", ephemeral=True)
            return
        bot.add_view(PanelView(bot))
        await bot.tree.sync(guild=bot.guild_obj)
        await interaction.followup.send(
            f"Panneau mis à jour — version **{bot.cfg.version}**, "
            f"{len(bot.cfg.actions)} actions.", ephemeral=True)

    # ------------------------------------------------------------------ run #
    @tree.command(description="Lancer une action Ansible")
    @app_commands.describe(
        action="L'action à lancer",
        limit="Restreindre à un hôte ou un groupe (option --limit)",
        simulation="Forcer le mode --check, sans rien modifier",
    )
    @app_commands.autocomplete(action=action_autocomplete)
    async def run(interaction: discord.Interaction, action: str,
                  limit: str | None = None, simulation: bool | None = None):
        await bot.trigger(interaction, action, limit=limit, check=simulation)

    # --------------------------------------------------------------- status #
    @tree.command(description="État du job en cours et des dernières exécutions")
    async def status(interaction: discord.Interaction):
        try:
            check_access(bot.cfg, interaction)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        emb = discord.Embed(title="État", colour=discord.Colour.blurple())
        cur = bot.jobs.current
        if cur:
            emb.add_field(
                name="En cours",
                value=f"**{cur.action.get('label')}** — {cur.elapsed:.0f}s\n"
                      f"par {cur.user.mention}\n`{cur.pretty_cmd[:300]}`",
                inline=False)
        else:
            emb.add_field(name="En cours", value="Rien en cours.", inline=False)
        if bot.jobs.history:
            lines = []
            for j in list(bot.jobs.history)[:5]:
                icon = j.status()[0].split()[0]
                ts = int(j.started_wall.timestamp())
                lines.append(f"{icon} **{j.action.get('label')}** — <t:{ts}:R>, {j.elapsed:.0f}s")
            emb.add_field(name="Historique", value="\n".join(lines), inline=False)
        emb.set_footer(text=f"{bot.cfg.panel.get('footer_note', 'Ansible')} • {bot.cfg.version}")
        await interaction.response.send_message(embed=emb, ephemeral=True)

    # --------------------------------------------------------------- cancel #
    @tree.command(description="Interrompre le job Ansible en cours")
    async def cancel(interaction: discord.Interaction):
        try:
            check_access(bot.cfg, interaction)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        cur = bot.jobs.current
        if not cur:
            await interaction.response.send_message("Rien à annuler.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🛑 Interruption de **{cur.action.get('label')}**…", ephemeral=True)
        await cur.cancel()

    # ----------------------------------------------------------------- logs #
    @tree.command(description="Récupérer le log complet d'une exécution récente")
    @app_commands.describe(index="0 = la plus récente (par défaut)")
    async def logs(interaction: discord.Interaction, index: int = 0):
        try:
            check_access(bot.cfg, interaction)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        files = sorted(bot.cfg.log_dir.glob("*.log"), reverse=True)
        if not files:
            await interaction.response.send_message("Aucun log disponible.", ephemeral=True)
            return
        if not 0 <= index < len(files):
            await interaction.response.send_message(
                f"Index hors limites (0 à {len(files) - 1}).", ephemeral=True)
            return
        await interaction.response.send_message(
            file=discord.File(files[index]), ephemeral=True)

    # ----------------------------------------------------------------- exec #
    @tree.command(description="Commande ad-hoc libre (désactivée par défaut dans la config)")
    @app_commands.describe(pattern="Hôte ou groupe", command="Commande shell à exécuter")
    async def exec(interaction: discord.Interaction, pattern: str, command: str):
        try:
            check_access(bot.cfg, interaction)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        if not bot.cfg.ansible.get("allow_exec", False):
            await interaction.response.send_message(
                "`/exec` est désactivé. Mets `ansible.allow_exec` à `true` dans config.json "
                "puis lance `/reload` si tu veux l'activer.", ephemeral=True)
            return
        adhoc = {
            "id": "exec", "label": f"exec sur {pattern}", "emoji": "⌨️",
            "type": "adhoc", "pattern": pattern, "module": "shell", "args": command,
            "become": True, "confirm": True,
            "confirm_text": "Commande libre exécutée en root sur les hôtes ciblés.",
            "timeout": 600,
        }
        bot.cfg.data["actions"].append(adhoc)
        try:
            await bot.trigger(interaction, "exec")
        finally:
            bot.cfg.data["actions"] = [a for a in bot.cfg.actions if a["id"] != "exec"]

    # --------------------------------------------------------------- reload #
    @tree.command(description="Recharger config.json sans toucher au panneau")
    async def reload(interaction: discord.Interaction):
        try:
            check_access(bot.cfg, interaction, enforce_channel=False)
        except Denied as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        try:
            bot.cfg.load()
        except Exception as exc:
            await interaction.response.send_message(f"❌ config.json invalide : `{exc}`",
                                                    ephemeral=True)
            return
        bot.add_view(PanelView(bot))
        await interaction.response.send_message(
            f"Config rechargée : {len(bot.cfg.actions)} actions, version {bot.cfg.version}.",
            ephemeral=True)

    # -------------------------------------------------------------- version #
    @tree.command(description="Version du bot et état de l'environnement Ansible")
    async def version(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                bot.cfg.ansible.get("playbook_cmd", "ansible-playbook"), "--version",
                cwd=str(bot.cfg.workdir) if bot.cfg.workdir.is_dir() else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            ansible_version = out.decode("utf-8", "replace").splitlines()[0]
        except Exception as exc:
            ansible_version = f"indisponible ({exc})"
        emb = discord.Embed(title="Ansible Control", colour=discord.Colour.blurple())
        emb.add_field(name="Bot", value=f"`{bot.cfg.version}`", inline=True)
        emb.add_field(name="discord.py", value=f"`{discord.__version__}`", inline=True)
        emb.add_field(name="Python", value=f"`{sys.version.split()[0]}`", inline=True)
        emb.add_field(name="Ansible", value=f"`{ansible_version}`", inline=False)
        emb.add_field(name="Répertoire", value=f"`{bot.cfg.workdir}`", inline=False)
        emb.add_field(name="Actions", value=str(len(bot.cfg.actions)), inline=True)
        emb.set_footer(text=f"{bot.cfg.panel.get('footer_note', 'Ansible')} • {bot.cfg.version}")
        await interaction.followup.send(embed=emb, ephemeral=True)

    # ----------------------------------------------------- erreurs globales #
    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("Erreur de commande", exc_info=error)
        msg = f"⛔ {error}" if isinstance(error, app_commands.CheckFailure) \
            else f"❌ Erreur interne : `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
            token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("Missing Discord token.")

    try:
        cfg = Config(CONFIG_PATH)
    except Exception as exc:
        sys.exit(f"config.json invalide : {exc}")

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    AnsibleBot(cfg).run(token, log_handler=None)


if __name__ == "__main__":
    main()
