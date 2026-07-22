"""Action « Nouvel identifiant de projet » du plugin Rev0.

Coupe l'héritage des revues : régénère `rev0.project_uid` dans le .kicad_pro
du projet ouvert. Les revues déjà soumises gardent l'ancien identifiant (et
leur groupe) ; les prochaines soumissions démarrent un nouveau groupe de
suivi dans Rev0.

Depuis KiCad : menu des plugins → « Rev0 — Nouvel identifiant de projet ».
En CLI :  python3 rev0_reset_project.py --project /chemin/du/projet
(équivalent : rev0_review.py --new-project-id, qui soumet en plus une revue).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rev0_review import KiCadContext, Rev0Error, ensure_project_uid, open_local_page, report_error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Régénérer l'identifiant Rev0 (rev0.project_uid) d'un projet KiCad"
    )
    parser.add_argument(
        "--project", default=None, help="Dossier du projet (défaut : projet ouvert dans KiCad)"
    )
    args = parser.parse_args()

    ctx = KiCadContext.connect()
    project_dir = Path(args.project).resolve() if args.project else ctx.project_dir
    if project_dir is None or not project_dir.is_dir():
        report_error(
            "Projet introuvable. Ouvrez et enregistrez un projet dans KiCad "
            "(ou passez --project en ligne de commande)."
        )
        return 1

    try:
        uid, project_file = ensure_project_uid(project_dir, ctx.project_name, force_new=True)
    except Rev0Error as error:
        report_error(str(error))
        return 1
    if uid is None or project_file is None:
        report_error(f"Aucun .kicad_pro trouvé dans {project_dir}.")
        return 1

    open_local_page(
        "Rev0 — nouvel identifiant de projet",
        f"{project_file.name} : nouvel identifiant {uid}.",
        "Les revues déjà soumises gardent l'ancien groupe ; la prochaine "
        "« Revue Rev0 » démarrera un nouveau suivi.",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
