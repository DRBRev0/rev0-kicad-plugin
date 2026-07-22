# Plugin KiCad — Rev0 Revue de design

Plugin [KiCad](https://www.kicad.org/) **IPC (KiCad 10+)** qui soumet votre projet en
**revue de design** à [Rev0](https://rev0.drb-conception.fr) (DRB Conception) sans
quitter KiCad — bouton « **Revue Rev0** » dans l'**éditeur de schéma** (et l'éditeur
de PCB).

Au clic :

1. **Connexion automatique** : pas de jeton à copier-coller. Au premier usage le
   plugin ouvre votre navigateur sur la page d'autorisation Rev0 ; vous vous
   connectez, cliquez « Autoriser », et le plugin récupère son jeton API tout seul.
2. **Suivi par projet** : le plugin pose une clé `"rev0": {"project_uid": "…"}` dans
   le `.kicad_pro` (une fois pour toutes). Toutes les demandes de revue du même
   projet sont ainsi reliées entre elles dans Rev0.
3. **Soumission et retour** : les `*.kicad_sch` du projet sont envoyés à l'API, puis
   la page de la revue s'ouvre dans le navigateur — conversion Zener, pauses
   composants inconnus, questions connecteurs et rapport IA s'y suivent en direct.

## Installation

Nécessite **KiCad 10** ou plus récent, avec l'API IPC activée
(*Préférences → Plugins → Activer le serveur d'API*, activé par défaut).

1. Copiez ce dépôt (ou une archive) dans le dossier des plugins KiCad :
   - Linux : `~/.local/share/kicad/10.0/plugins/fr.drb-conception.rev0/`
   - macOS : `~/Documents/KiCad/10.0/plugins/fr.drb-conception.rev0/`
   - Windows : `%USERPROFILE%\Documents\KiCad\10.0\plugins\fr.drb-conception.rev0\`

   Le `plugin.json` doit être à la racine de ce dossier.
2. Redémarrez KiCad. Au premier lancement, KiCad crée l'environnement Python du
   plugin et installe `kicad-python` (voir `requirements.txt`).
3. Le bouton « **Revue Rev0** » apparaît dans la barre d'outils de l'éditeur de
   schéma et de l'éditeur de PCB.

## Utilisation

Ouvrez votre projet, enregistrez vos schémas, puis cliquez sur « **Revue Rev0** »
dans l'éditeur de schéma. C'est tout : autorisation par navigateur au premier
usage, puis la page de la revue s'ouvre à chaque soumission.

Rappel : si un composant n'est pas encore qualifié par Rev0, la revue est mise en
pause le temps de le qualifier ; les connecteurs demandent vos réponses
(interface, tension, courant) dans l'application.

### En ligne de commande (sans KiCad)

```bash
python3 rev0_review.py --project /chemin/du/projet [--name "Ma carte"]
```

Python 3.9+, uniquement la bibliothèque standard (l'API IPC n'est utilisée que
depuis KiCad). Le flux de connexion par navigateur fonctionne aussi en CLI.

## Configuration

Tout est automatique. Le jeton est stocké dans le dossier de configuration que
KiCad attribue au plugin (API `get_plugin_settings_path`), ou `~/.rev0/config.json`
en CLI. Pour pointer une autre instance :

```json
{ "base_url": "https://rev0.example.com" }
```

Variables d'environnement : `REV0_BASE_URL`, `REV0_TOKEN` (prioritaires).
Les jetons se révoquent dans Rev0, page « Revues de design ».

## API utilisée

| Méthode | Route | Rôle |
|---|---|---|
| `POST` | `/api/v1/plugin/auth/start` | Ouvre une demande d'autorisation (sans auth) |
| `POST` | `/api/v1/plugin/auth/poll` | Le plugin récupère son jeton une fois la demande approuvée |
| `POST` | `/api/v1/reviews` | Crée une revue (`name`, `files[]`, `source`, `projectUid`) |
| `GET`  | `/api/v1/reviews` | Liste des revues (`?projectUid=` pour un projet) |
| `GET`  | `/api/v1/reviews/:id` | Avancement d'une revue |

Authentification des routes `reviews` : `Authorization: Bearer <token>`.

## Licence

MIT — voir [LICENSE](LICENSE).
