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
  3. Un devis est demandé à POST /api/v1/reviews/quote (taille mesurée du
     design → prix en crédits) et la voie est choisie : payante (immédiate)
     si le solde couvre, sinon file gratuite (lente) si disponible —
     forçable avec --lane.
  4. Les .kicad_sch du projet sont envoyés à POST /api/v1/reviews, puis la
     page de la revue s'ouvre dans le navigateur : conversion, questions
     connecteurs et rapport IA s'y suivent en direct. Pour la file gratuite,
     cette page ouverte sert de signe de présence : la fermer trop longtemps
     retire la revue de la file.

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

PLUGIN_VERSION = "2.1.0"
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
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        detail = body.get("error", raw) if isinstance(body, dict) else raw
        raise Rev0HttpError(error.code, str(detail), body if isinstance(body, dict) else {}) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise Rev0Error(f"Impossible de joindre Rev0 ({base_url}) : {error}") from error


class Rev0HttpError(Rev0Error):
    def __init__(self, status: int, detail: str, body: dict | None = None) -> None:
        super().__init__(f"Rev0 a répondu HTTP {status} : {detail}")
        self.status = status
        self.detail = detail
        self.body = body or {}


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


def read_project_file(project_file: Path) -> dict:
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Rev0Error(f"Lecture de {project_file.name} impossible : {error}") from error
    if not isinstance(data, dict):
        raise Rev0Error(f"{project_file.name} n'a pas la structure attendue.")
    return data


def write_project_uid(project_file: Path, uid: str) -> None:
    """Pose rev0.project_uid dans le .kicad_pro en préservant tout le reste."""
    data = read_project_file(project_file)
    if not isinstance(data.get("rev0"), dict):
        data["rev0"] = {}
    data["rev0"]["project_uid"] = uid
    try:
        project_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise Rev0Error(f"Écriture de {project_file.name} impossible : {error}") from error


def ensure_project_uid(
    project_dir: Path, project_name: str | None, force_new: bool = False
) -> tuple[str | None, Path | None]:
    """Identifiant du projet + fichier .kicad_pro. `force_new` coupe
    l'héritage : un nouvel identifiant remplace l'existant (nouveau groupe de
    suivi côté Rev0)."""
    project_file = find_project_file(project_dir, project_name)
    if project_file is None:
        print("Avertissement : aucun .kicad_pro trouvé — revue soumise sans identifiant projet.")
        return None, None

    if not force_new:
        section = read_project_file(project_file).get("rev0")
        if isinstance(section, dict) and isinstance(section.get("project_uid"), str):
            return section["project_uid"], project_file

    uid = str(uuid.uuid4())
    write_project_uid(project_file, uid)
    verb = "réinitialisé" if force_new else "créé"
    print(f"Identifiant projet {verb} dans {project_file.name} : {uid}")
    return uid, project_file


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


def format_wait(hours: object) -> str:
    try:
        value = float(hours)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "inconnue"
    if value < 1:
        return f"~{max(5, round(value * 60))} min"
    return f"~{value:g} h"


def choose_lane(base_url: str, token: str, files: list[dict], lane_arg: str) -> str:
    """Devis AVANT envoi (taille mesurée → prix) puis choix de la voie :
    payante (immédiate) si le solde couvre, sinon file gratuite (lente).
    `lane_arg` force une voie ('credits'/'free'), 'auto' laisse décider."""
    quote = api_request(base_url, "POST", "/api/v1/reviews/quote", {"files": files}, token=token)

    price = quote.get("quote", {})
    tier = price.get("tier", "?")
    credits_price = price.get("credits", "?")
    balance = quote.get("balance", 0)
    sufficient = bool(quote.get("credits", {}).get("sufficient"))
    free_lane = quote.get("freeLane", {})
    free_available = bool(free_lane.get("available"))

    print(
        f"Devis Rev0 : taille {tier} — {credits_price} crédit(s) "
        f"({price.get('sheetCount', '?')} feuille(s), {price.get('componentCount', '?')} composant(s)). "
        f"Solde : {balance}."
    )

    if quote.get("isAdmin"):
        return "credits"  # non facturé côté serveur

    if lane_arg == "credits":
        if not sufficient:
            raise Rev0Error(
                f"Solde insuffisant ({balance}/{credits_price} crédits). "
                f"Rechargez sur {base_url}/credits ou relancez avec --lane free."
            )
        return "credits"

    if lane_arg == "free":
        if not free_available:
            queued = free_lane.get("queuedReviewId")
            if queued:
                raise Rev0Error(
                    f"Vous avez déjà une revue dans la file gratuite (#{queued}) — une seule à "
                    f"la fois. Suivi : {base_url}/reviews/{queued}"
                )
            raise Rev0Error("La file gratuite est fermée pour le moment.")
        print(f"File gratuite : attente estimée {format_wait(free_lane.get('estimatedWaitHours'))}.")
        return "free"

    # auto : payant si couvrable, sinon file gratuite.
    if sufficient:
        print(f"Voie immédiate : {credits_price} crédit(s) seront débités (remboursés si échec).")
        return "credits"
    if free_available:
        print(
            "Solde insuffisant pour la voie immédiate → file gratuite, attente estimée "
            f"{format_wait(free_lane.get('estimatedWaitHours'))}."
        )
        return "free"

    queued = free_lane.get("queuedReviewId")
    extra = (
        f" Vous avez déjà une revue dans la file gratuite (#{queued}) : {base_url}/reviews/{queued}."
        if queued
        else ""
    )
    raise Rev0Error(
        f"Aucune voie disponible : solde insuffisant ({balance}/{credits_price} crédits) et file "
        f"gratuite indisponible.{extra} Rechargez sur {base_url}/credits."
    )


def submit_review(
    base_url: str,
    token: str,
    ctx: KiCadContext,
    files: list[dict],
    name: str,
    project_uid: str | None,
    lane: str,
) -> dict:
    source_meta = {"plugin": "rev0-kicad", "version": PLUGIN_VERSION, "host": socket.gethostname()}
    if ctx.kicad_version:
        source_meta["kicadVersion"] = ctx.kicad_version

    payload = {
        "name": name,
        "source": "plugin",
        "sourceMeta": source_meta,
        "files": files,
        "lane": lane,
    }
    if project_uid:
        payload["projectUid"] = project_uid
    return api_request(base_url, "POST", "/api/v1/reviews", payload, token=token)


def run(
    ctx: KiCadContext,
    project_dir: Path,
    name: str,
    new_project_id: bool = False,
    lane_arg: str = "auto",
) -> str:
    """Déroulé complet : jeton (login navigateur si besoin), UID projet,
    devis + choix de voie, soumission, ouverture de la revue. Retourne
    l'URL de la revue."""
    base_url = load_config(ctx)["base_url"]
    token = ensure_token(base_url, ctx, name)
    project_uid, project_file = ensure_project_uid(
        project_dir, ctx.project_name, force_new=new_project_id
    )

    files = collect_schematics(project_dir, ctx.project_name)
    if not files:
        raise Rev0Error(
            f"Aucun fichier .kicad_sch trouvé dans {project_dir}. "
            "Enregistrez le schéma avant de lancer la revue."
        )

    def quote_and_submit() -> dict:
        lane = choose_lane(base_url, token, files, lane_arg)
        try:
            return submit_review(base_url, token, ctx, files, name, project_uid, lane)
        except Rev0HttpError as error:
            # L'état a pu changer entre devis et envoi : messages actionnables.
            if error.status == 402:
                raise Rev0Error(
                    f"Crédits insuffisants ({error.body.get('balance', '?')}/"
                    f"{error.body.get('requiredCredits', '?')}). Rechargez sur "
                    f"{base_url}/credits ou relancez avec --lane free."
                ) from error
            if error.status == 409:
                queued = error.body.get("queuedReviewId")
                raise Rev0Error(
                    "Vous avez déjà une revue dans la file gratuite — une seule à la fois."
                    + (f" Suivi : {base_url}/reviews/{queued}" if queued else "")
                ) from error
            raise

    try:
        result = quote_and_submit()
    except Rev0HttpError as error:
        if error.status != 401:
            raise
        # Jeton révoqué ou périmé : on le jette et on refait un login.
        save_token(ctx, base_url, None)
        token = browser_login(base_url, ctx, name)
        save_token(ctx, base_url, token)
        result = quote_and_submit()

    # Le serveur fait autorité sur l'identifiant : s'il en renvoie un autre
    # (uid appartenant à un autre compte — projet partagé/copié), le
    # .kicad_pro est mis à jour pour que le suivi reparte proprement.
    server_uid = result.get("projectUid")
    if server_uid and project_uid and server_uid != project_uid and project_file:
        write_project_uid(project_file, server_uid)
        print(
            "Identifiant projet réattribué par Rev0 (projet partagé ?) : "
            f"{project_file.name} mis à jour → {server_uid}"
        )

    review_url = f"{base_url}{result['url']}"
    if result.get("status") == "free_queued":
        print(
            f"Revue #{result['id']} en file gratuite — suivi : {review_url}\n"
            "Gardez la page de la revue OUVERTE dans le navigateur : elle sert de signe de "
            "présence (une absence prolongée retire la revue de la file ; une coupure courte "
            "est tolérée)."
        )
    else:
        print(f"Revue #{result['id']} créée — suivi : {review_url}")
    webbrowser.open(review_url)
    return review_url


# ---------------------------------------------------------------------------
# Retour utilisateur hors soumission : lancé depuis KiCad il n'y a pas de
# console, on ouvre donc une petite page locale dans le navigateur.
# ---------------------------------------------------------------------------
def open_local_page(title: str, message: str, hint: str = "") -> None:
    if not os.environ.get("KICAD_API_SOCKET"):
        return
    body = (
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style='font-family:system-ui;max-width:560px;margin:80px auto;padding:0 24px'>"
        f"<h1 style='font-size:22px'>{html.escape(title)}</h1>"
        f"<p style='white-space:pre-wrap'>{html.escape(message)}</p>"
        f"{f'<p>{html.escape(hint)}</p>' if hint else ''}"
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


def report_error(message: str) -> None:
    print(f"Rev0 : {message}", file=sys.stderr)
    open_local_page(
        "Rev0 — la revue n'a pas pu partir",
        message,
        "Corrigez puis recliquez sur « Revue Rev0 » dans KiCad.",
    )


# ---------------------------------------------------------------------------
# Entrée : bouton KiCad (sans arguments, projet via l'API IPC) ou CLI.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Soumettre un projet KiCad en revue Rev0")
    parser.add_argument("--project", default=None, help="Dossier du projet (défaut : projet ouvert dans KiCad)")
    parser.add_argument("--name", default=None, help="Nom de la revue (défaut : nom du projet)")
    parser.add_argument(
        "--new-project-id",
        action="store_true",
        help="Coupe l'héritage : régénère l'identifiant rev0.project_uid du "
        ".kicad_pro avant de soumettre (nouveau groupe de suivi dans Rev0)",
    )
    parser.add_argument(
        "--lane",
        choices=("auto", "credits", "free"),
        default="auto",
        help="Voie de soumission : credits (immédiate, débite le solde), free "
        "(file gratuite lente), auto (défaut : credits si le solde couvre, "
        "sinon free)",
    )
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
        run(ctx, project_dir, name, new_project_id=args.new_project_id, lane_arg=args.lane)
    except Rev0Error as error:
        report_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
