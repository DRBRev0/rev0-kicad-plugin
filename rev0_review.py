"""Plugin KiCad Rev0 — soumettre le projet ouvert en revue de design.

Plugin IPC KiCad 10+ (bouton « Revue Rev0 » dans l'éditeur de schéma et
l'éditeur de PCB). Tout ce que l'API IPC sait faire passe par elle (kipy) :
projet ouvert, version de KiCad, dossier de configuration du plugin. Le
système de fichiers n'est utilisé que pour ce que l'API ne couvre pas :
lecture des .kicad_sch et écriture de l'identifiant projet dans le .kicad_pro.

Déroulé d'un clic sur le bouton :

  1. Connexion : si aucun jeton API n'est enregistré (ou jeton révoqué), le
     plugin ouvre le navigateur sur la page d'autorisation Rev0 et attend
     l'approbation (flux type "device flow" — aucun copier-coller de jeton).
  2. Identifiant projet : une clé  "rev0": {"project_uid": "…"}  est posée
     dans le .kicad_pro (une fois pour toutes) afin de relier entre elles
     les demandes de revue successives du même projet.
  3. Les .kicad_sch du projet sont envoyés à POST /api/v1/reviews, puis la
     page de la revue s'ouvre dans le navigateur : conversion, questions
     connecteurs et rapport IA s'y suivent en direct.

Mode ligne de commande (sans KiCad) :
    python3 rev0_review.py --project /chemin/du/projet [--name "Ma carte"]

Configuration : gérée automatiquement (login navigateur). Le fichier de
configuration vit dans le dossier de settings attribué par KiCad au plugin
(API get_plugin_settings_path), avec repli sur ~/.rev0/config.json en CLI.
Variables d'environnement : REV0_BASE_URL, REV0_TOKEN.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

PLUGIN_VERSION = "2.0.0"
PLUGIN_IDENTIFIER = "fr.drb-conception.rev0"
DEFAULT_BASE_URL = "https://rev0.drb-conception.fr"
LEGACY_CONFIG_PATH = Path.home() / ".rev0" / "config.json"
HTTP_TIMEOUT = 60


class Rev0Error(RuntimeError):
    """Erreur à présenter à l'utilisateur (message déjà rédigé)."""


# ---------------------------------------------------------------------------
# Contexte KiCad via l'API IPC (kipy) — source de vérité quand on est lancé
# depuis KiCad ; tout est optionnel pour garder le mode CLI.
# ---------------------------------------------------------------------------
class KiCadContext:
    def __init__(self) -> None:
        self.connected = False
        self.project_dir: Path | None = None
        self.project_name: str | None = None
        self.kicad_version: str | None = None
        self.settings_dir: Path | None = None

    @classmethod
    def connect(cls) -> "KiCadContext":
        ctx = cls()
        if not os.environ.get("KICAD_API_SOCKET"):
            return ctx
        try:
            from kipy import KiCad
            from kipy.proto.common.types import DocumentType
        except ImportError:
            return ctx

        try:
            kicad = KiCad(client_name=f"rev0-{os.getpid()}")
            ctx.kicad_version = kicad.get_version().full_version
            ctx.settings_dir = Path(kicad.get_plugin_settings_path(PLUGIN_IDENTIFIER))

            documents = list(kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC))
            if not documents:
                documents = list(kicad.get_open_documents(DocumentType.DOCTYPE_PCB))
            for document in documents:
                project = document.project
                if project.path:
                    ctx.project_dir = Path(project.path)
                    ctx.project_name = project.name or None
                    break
            ctx.connected = True
        except Exception:
            # API indisponible (vieux KiCad, serveur coupé…) : on continue en
            # mode dégradé, le reste du plugin a des replis.
            pass
        return ctx


# ---------------------------------------------------------------------------
# Configuration (jeton + URL) — dossier de settings KiCad d'abord, legacy
# ~/.rev0/config.json ensuite, variables d'environnement par-dessus tout.
# ---------------------------------------------------------------------------
def config_paths(ctx: KiCadContext) -> list[Path]:
    paths = []
    if ctx.settings_dir:
        paths.append(ctx.settings_dir / "config.json")
    paths.append(LEGACY_CONFIG_PATH)
    return paths


def load_config(ctx: KiCadContext) -> dict:
    config: dict = {}
    for path in reversed(config_paths(ctx)):
        if path.is_file():
            try:
                config.update(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    if os.environ.get("REV0_BASE_URL"):
        config["base_url"] = os.environ["REV0_BASE_URL"]
    if os.environ.get("REV0_TOKEN"):
        config["token"] = os.environ["REV0_TOKEN"]
    config.setdefault("base_url", DEFAULT_BASE_URL)
    config["base_url"] = str(config["base_url"]).rstrip("/")
    return config


def save_token(ctx: KiCadContext, base_url: str, token: str | None) -> None:
    path = config_paths(ctx)[0]
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data["base_url"] = base_url
    if token is None:
        data.pop("token", None)
    else:
        data["token"] = token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def api_request(
    base_url: str, method: str, path: str, payload: dict | None = None, token: str | None = None
) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("error", body)
        except ValueError:
            detail = body
        raise Rev0HttpError(error.code, str(detail)) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise Rev0Error(f"Impossible de joindre Rev0 ({base_url}) : {error}") from error


class Rev0HttpError(Rev0Error):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Rev0 a répondu HTTP {status} : {detail}")
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Connexion par navigateur (flux d'autorisation, aucun jeton à copier-coller)
# ---------------------------------------------------------------------------
def browser_login(base_url: str, ctx: KiCadContext, project_name: str | None) -> str:
    meta = {
        "host": socket.gethostname()[:120],
        "pluginVersion": PLUGIN_VERSION,
    }
    if ctx.kicad_version:
        meta["kicadVersion"] = ctx.kicad_version[:60]
    if project_name:
        meta["projectName"] = project_name[:150]

    start = api_request(base_url, "POST", "/api/v1/plugin/auth/start", meta)
    authorize_url = start["authorizeUrl"]
    print(f"Autorisation requise — ouverture du navigateur : {authorize_url}")
    webbrowser.open(authorize_url)

    interval = max(1, int(start.get("pollIntervalSeconds", 3)))
    deadline = time.time() + int(start.get("expiresInMinutes", 10)) * 60
    payload = {"code": start["code"], "secret": start["secret"]}
    while time.time() < deadline:
        time.sleep(interval)
        result = api_request(base_url, "POST", "/api/v1/plugin/auth/poll", payload)
        status = result.get("status")
        if status == "approved":
            user = result.get("userEmail") or "votre compte"
            print(f"Plugin autorisé pour {user}.")
            return result["token"]
        if status == "denied":
            raise Rev0Error("Demande refusée dans l'application Rev0.")
        if status in ("expired", "unknown"):
            raise Rev0Error(
                "La demande d'autorisation a expiré. Relancez la revue depuis KiCad."
            )
        # pending : on continue à attendre.
    raise Rev0Error("Délai d'autorisation dépassé (10 min). Relancez la revue depuis KiCad.")


def ensure_token(base_url: str, ctx: KiCadContext, project_name: str | None) -> str:
    config = load_config(ctx)
    token = config.get("token")
    if token:
        return token
    token = browser_login(base_url, ctx, project_name)
    save_token(ctx, base_url, token)
    return token


# ---------------------------------------------------------------------------
# Identifiant unique du projet dans le .kicad_pro (clé "rev0.project_uid") —
# pas d'API IPC pour le fichier projet : édition JSON directe, en préservant
# tout le contenu existant.
# ---------------------------------------------------------------------------
def find_project_file(project_dir: Path, project_name: str | None) -> Path | None:
    if project_name:
        candidate = project_dir / f"{project_name}.kicad_pro"
        if candidate.is_file():
            return candidate
    candidates = sorted(project_dir.glob("*.kicad_pro"))
    return candidates[0] if candidates else None


def ensure_project_uid(project_dir: Path, project_name: str | None) -> str | None:
    project_file = find_project_file(project_dir, project_name)
    if project_file is None:
        print("Avertissement : aucun .kicad_pro trouvé — revue soumise sans identifiant projet.")
        return None
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Rev0Error(f"Lecture de {project_file.name} impossible : {error}") from error
    if not isinstance(data, dict):
        raise Rev0Error(f"{project_file.name} n'a pas la structure attendue.")

    section = data.get("rev0")
    if isinstance(section, dict) and isinstance(section.get("project_uid"), str):
        return section["project_uid"]

    uid = str(uuid.uuid4())
    data.setdefault("rev0", {})
    if not isinstance(data["rev0"], dict):
        data["rev0"] = {}
    data["rev0"]["project_uid"] = uid
    try:
        project_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise Rev0Error(f"Écriture de {project_file.name} impossible : {error}") from error
    print(f"Identifiant projet créé dans {project_file.name} : {uid}")
    return uid


# ---------------------------------------------------------------------------
# Collecte et soumission
# ---------------------------------------------------------------------------
def collect_schematics(project_dir: Path, project_name: str | None = None) -> list[dict]:
    """Tous les .kicad_sch du projet (récursif, dossiers de backup exclus).
    La feuille racine (même nom que le projet, à la racine) passe en premier :
    Rev0 marque le premier fichier reçu comme feuille racine."""
    root = project_dir / f"{project_name}.kicad_sch" if project_name else None
    paths = [
        path
        for path in sorted(project_dir.rglob("*.kicad_sch"))
        if not any(part.endswith("-backups") for part in path.parts)
    ]
    paths.sort(key=lambda p: 0 if root is not None and p == root else 1)
    return [{"name": p.name, "content": p.read_text(encoding="utf-8")} for p in paths]


def submit_review(
    base_url: str, token: str, ctx: KiCadContext, project_dir: Path, name: str
) -> dict:
    files = collect_schematics(project_dir, ctx.project_name)
    if not files:
        raise Rev0Error(
            f"Aucun fichier .kicad_sch trouvé dans {project_dir}. "
            "Enregistrez le schéma avant de lancer la revue."
        )

    project_uid = ensure_project_uid(project_dir, ctx.project_name)
    source_meta = {"plugin": "rev0-kicad", "version": PLUGIN_VERSION, "host": socket.gethostname()}
    if ctx.kicad_version:
        source_meta["kicadVersion"] = ctx.kicad_version

    payload = {
        "name": name,
        "source": "plugin",
        "sourceMeta": source_meta,
        "files": files,
    }
    if project_uid:
        payload["projectUid"] = project_uid
    return api_request(base_url, "POST", "/api/v1/reviews", payload, token=token)


def run(ctx: KiCadContext, project_dir: Path, name: str) -> str:
    """Déroulé complet : jeton (login navigateur si besoin), UID projet,
    soumission, ouverture de la revue. Retourne l'URL de la revue."""
    base_url = load_config(ctx)["base_url"]
    token = ensure_token(base_url, ctx, name)

    try:
        result = submit_review(base_url, token, ctx, project_dir, name)
    except Rev0HttpError as error:
        if error.status != 401:
            raise
        # Jeton révoqué ou périmé : on le jette et on refait un login.
        save_token(ctx, base_url, None)
        token = browser_login(base_url, ctx, name)
        save_token(ctx, base_url, token)
        result = submit_review(base_url, token, ctx, project_dir, name)

    review_url = f"{base_url}{result['url']}"
    print(f"Revue #{result['id']} créée — suivi : {review_url}")
    webbrowser.open(review_url)
    return review_url


# ---------------------------------------------------------------------------
# Retour utilisateur en cas d'erreur : lancé depuis KiCad il n'y a pas de
# console, on ouvre donc une petite page locale dans le navigateur.
# ---------------------------------------------------------------------------
def report_error(message: str) -> None:
    print(f"Rev0 : {message}", file=sys.stderr)
    if not os.environ.get("KICAD_API_SOCKET"):
        return
    body = (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        "<title>Rev0 — erreur</title></head>"
        "<body style='font-family:system-ui;max-width:560px;margin:80px auto;padding:0 24px'>"
        "<h1 style='font-size:22px'>Rev0 — la revue n'a pas pu partir</h1>"
        f"<p style='white-space:pre-wrap'>{html.escape(message)}</p>"
        "<p>Corrigez puis recliquez sur « Revue Rev0 » dans KiCad.</p>"
        "</body></html>"
    )
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".html", prefix="rev0-", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(body)
        webbrowser.open(Path(handle.name).as_uri())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entrée : bouton KiCad (sans arguments, projet via l'API IPC) ou CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Soumettre un projet KiCad en revue Rev0")
    parser.add_argument("--project", default=None, help="Dossier du projet (défaut : projet ouvert dans KiCad)")
    parser.add_argument("--name", default=None, help="Nom de la revue (défaut : nom du projet)")
    args = parser.parse_args()

    ctx = KiCadContext.connect()

    project_dir: Path | None = None
    if args.project:
        project_dir = Path(args.project).resolve()
    elif ctx.project_dir:
        project_dir = ctx.project_dir
    if project_dir is None or not project_dir.is_dir():
        report_error(
            "Projet introuvable. Ouvrez et enregistrez un projet dans KiCad "
            "(ou passez --project en ligne de commande)."
        )
        return 1

    name = args.name or ctx.project_name or project_dir.name
    try:
        run(ctx, project_dir, name)
    except Rev0Error as error:
        report_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
