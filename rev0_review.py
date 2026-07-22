"""Plugin KiCad Rev0 — soumettre le projet courant en revue de design.

Deux modes d'utilisation :

1. Plugin pcbnew (éditeur de PCB KiCad) : copier ce fichier dans le dossier
   de plugins KiCad (Outils → Plugins externes → Ouvrir le dossier des
   plugins), puis « Rev0 — Revue de design » apparaît dans le menu Outils.
   Le plugin collecte tous les .kicad_sch du projet ouvert et les envoie.

2. Ligne de commande (sans KiCad) :
       python3 rev0_review.py --project /chemin/du/projet --name "Ma carte"

Configuration (fichier ~/.rev0/config.json ou variables d'environnement) :
    {"base_url": "https://rev0.example.com", "token": "rev0_..."}
    REV0_BASE_URL / REV0_TOKEN

Le jeton API se génère dans l'application Rev0, page « Revues de design ».
L'API répond avec l'identifiant de la revue ; son avancement (conversion
Zener, pause composants inconnus, questions connecteurs, rapport IA) se suit
dans l'application ou via GET /api/v1/reviews/<id>.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_VERSION = "1.0.0"


def load_config() -> dict:
    config: dict = {}
    config_path = Path.home() / ".rev0" / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if os.environ.get("REV0_BASE_URL"):
        config["base_url"] = os.environ["REV0_BASE_URL"]
    if os.environ.get("REV0_TOKEN"):
        config["token"] = os.environ["REV0_TOKEN"]
    return config


def collect_schematics(project_dir: Path) -> list[dict]:
    """Tous les .kicad_sch du projet (récursif, dossiers de backup exclus)."""
    files = []
    for path in sorted(project_dir.rglob("*.kicad_sch")):
        if any(part.endswith("-backups") for part in path.parts):
            continue
        files.append(
            {
                "name": path.name,
                "content": path.read_text(encoding="utf-8"),
            }
        )
    return files


def submit_review(project_dir: Path, name: str) -> dict:
    config = load_config()
    base_url = (config.get("base_url") or "").rstrip("/")
    token = config.get("token") or ""
    if not base_url or not token:
        raise RuntimeError(
            "Configuration manquante : renseignez base_url et token dans "
            "~/.rev0/config.json (ou REV0_BASE_URL / REV0_TOKEN). "
            "Le jeton se génère dans Rev0, page « Revues de design »."
        )

    files = collect_schematics(project_dir)
    if not files:
        raise RuntimeError(f"Aucun fichier .kicad_sch trouvé dans {project_dir}")

    payload = {
        "name": name,
        "source": "plugin",
        "sourceMeta": {"plugin": "rev0-kicad", "version": PLUGIN_VERSION},
        "files": files,
    }
    request = urllib.request.Request(
        f"{base_url}/api/v1/reviews",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Rev0 a refusé la soumission (HTTP {error.code}) : {body}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Soumettre un projet KiCad en revue Rev0")
    parser.add_argument("--project", required=True, help="Dossier du projet KiCad")
    parser.add_argument("--name", default=None, help="Nom de la revue (défaut : nom du dossier)")
    args = parser.parse_args()

    project_dir = Path(args.project).resolve()
    name = args.name or project_dir.name
    result = submit_review(project_dir, name)
    print(f"Revue #{result['id']} créée — suivez-la sur {result['url']}")
    return 0


# ---------------------------------------------------------------------------
# Intégration pcbnew (optionnelle : le module n'existe qu'à l'intérieur de KiCad)
# ---------------------------------------------------------------------------
try:
    import pcbnew  # type: ignore
    import wx  # type: ignore

    class Rev0ReviewPlugin(pcbnew.ActionPlugin):  # pragma: no cover - UI KiCad
        def defaults(self):
            self.name = "Rev0 — Revue de design"
            self.category = "Rev0"
            self.description = (
                "Envoie les schémas du projet courant en revue de design Rev0 "
                "(analyse complète du design par l'IA spécialisée Rev0)."
            )
            self.show_toolbar_button = True

        def Run(self):
            board_path = pcbnew.GetBoard().GetFileName()
            if not board_path:
                wx.MessageBox("Enregistrez d'abord le projet.", "Rev0", wx.ICON_WARNING)
                return
            project_dir = Path(board_path).parent
            try:
                result = submit_review(project_dir, project_dir.name)
            except RuntimeError as error:
                wx.MessageBox(str(error), "Rev0 — erreur", wx.ICON_ERROR)
                return
            wx.MessageBox(
                f"Revue #{result['id']} créée.\nSuivez son avancement sur {result['url']}\n\n"
                "Rappel : si un composant n'est pas encore qualifié par Rev0, la revue sera "
                "mise en pause le temps de le qualifier ; les connecteurs demandent vos "
                "réponses (interface, tension, courant).",
                "Rev0 — revue soumise",
                wx.ICON_INFORMATION,
            )

    Rev0ReviewPlugin().register()
except ImportError:
    pass


if __name__ == "__main__":
    sys.exit(main())
