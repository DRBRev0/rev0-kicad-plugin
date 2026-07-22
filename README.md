# Plugin KiCad — Rev0 Revue de design

Plugin [KiCad](https://www.kicad.org/) qui soumet votre projet en **revue de design**
à [Rev0](https://rev0.drb-conception.fr) (DRB Conception) sans quitter KiCad.

Le plugin collecte tous les schémas (`*.kicad_sch`) du projet ouvert et les envoie à
l'API Rev0. La revue (conversion, vérifications, questions connecteurs, rapport IA)
se suit ensuite dans l'application Rev0.

## Installation

### Comme plugin KiCad (menu Outils)

1. Dans KiCad (éditeur de PCB), **Outils → Plugins externes → Ouvrir le dossier des plugins**.
2. Copiez-y `rev0_review.py`.
3. **Outils → Plugins externes → Rafraîchir** (ou redémarrez KiCad).
4. « **Rev0 — Revue de design** » apparaît dans le menu Outils.

### En ligne de commande (sans KiCad)

```bash
python3 rev0_review.py --project /chemin/du/projet --name "Ma carte"
```

Nécessite Python 3.9+ (aucune dépendance externe, uniquement la bibliothèque standard).

## Configuration

Renseignez l'URL de Rev0 et votre **jeton API** — soit dans `~/.rev0/config.json` :

```json
{
  "base_url": "https://rev0.drb-conception.fr",
  "token": "rev0_..."
}
```

…soit via les variables d'environnement `REV0_BASE_URL` et `REV0_TOKEN`.

> Le jeton se génère dans l'application Rev0, page **« Revues de design »**.
> Sa valeur n'est affichée qu'une seule fois.

## Utilisation

- **Depuis KiCad** : ouvrez votre projet, puis **Outils → Rev0 — Revue de design**.
- **En CLI** : voir ci-dessus.

Le plugin renvoie l'identifiant et l'URL de la revue créée. Si un composant n'est pas
encore qualifié par Rev0, la revue est mise en pause le temps de le qualifier ; les
connecteurs peuvent demander vos réponses (interface, tension, courant).

## API utilisée

| Méthode | Route | Rôle |
|---|---|---|
| `POST` | `/api/v1/reviews` | Crée une revue (corps : `name`, `files[]`, `source`) |
| `GET`  | `/api/v1/reviews/:id` | Avancement d'une revue |

Authentification : en-tête `Authorization: Bearer <token>`.

## Licence

MIT — voir [LICENSE](LICENSE).
