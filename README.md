# ikea

## Scraper IKEA "Dernière chance"

Ce dépôt contient un script Python pour extraire les produits de la page
"Dernière chance" IKEA (Canada FR) et un workflow GitHub Actions qui se
déclenche manuellement.

### Exécution locale

```bash
python scrape_ikea_last_chance.py \
  --url "https://www.ikea.com/ca/fr/cat/last-chance/?filters=f-availability%3AAVAILABLE_IN_STORE" \
  --output ikea_last_chance.json
```

### Workflow GitHub Actions

Le workflow `Scrape IKEA Last Chance` est disponible via l'onglet
**Actions** et se lance avec **Run workflow**. Il génère `ikea_last_chance.json`
et l'attache comme artifact.
