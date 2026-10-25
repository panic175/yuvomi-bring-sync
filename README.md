# yuvomi-bring-sync

Zwei-Wege-Brücke zwischen den Einkaufslisten von **Yuvomi** und **Bring!**.
Läuft als eigener Container auf dem Docker-Host, ohne eigene Ports, und spiegelt beide
Seiten im Takt von 10 Sekunden.

## Was synchronisiert wird

| | Bring → Yuvomi | Yuvomi → Bring |
|---|---|---|
| neuer Artikel | `POST /shopping/:id/items`, Kategorie aus dem Bring-Produktkatalog | `save_item` (Menge als `spec`) |
| Name / Menge | `PATCH /shopping/items/:id` (Yuvomi gewinnt bei gleichzeitiger Änderung) | `update_item` |
| abgehakt | in `items.recently` → `is_checked=1` (Bring gewinnt) | `complete_item` bzw. `ADD` beim Ent-haken |
| gelöscht | nur bei bestätigtem Fehlen und nur wenn offen | `DELETE` → `remove_item` |
| Listen | Bring-Liste → Yuvomi-Liste, gematcht über den Namen, fehlende wird angelegt | – |

Kategorien: `https://web.getbring.com/locale/catalog.<locale>.json` ist öffentlich
und liefert 13 Sektionen mit ~330 Produkten. Die Abbildung auf die Yuvomi-Kategorien
steht in `config.json` (`category_map`); Sektionen ohne Eintrag werden in Yuvomi
automatisch als Kategorie angelegt (`create_missing_categories`).

## Dateien

| Datei | Zweck |
|---|---|
| `bridge.py` | Die Brücke (Planner ist rein rechnerisch, daher ohne Netz testbar) |
| `test_bridge.py` | Offline-Selbstcheck: `python3 -m unittest -v test_bridge` |
| `config.json` | Stillgelegte Listen + Kategorie-Mapping |
| `Dockerfile`, `docker-compose.yml` | Betrieb auf dem Docker-Host |
| `requirements.txt` | Gepinnte `bring-api`-Version |

## Deployment (Docker-Host)

```bash
mkdir -p /opt/yuvomi-bring
cp bridge.py config.json Dockerfile docker-compose.yml requirements.txt /opt/yuvomi-bring/
cd /opt/yuvomi-bring
cat > .env <<'EOF'
BRING_USERNAME=...
BRING_PASSWORD=...
YUVOMI_TOKEN=...
EOF
chmod 600 .env
docker compose up -d --build
```

`.env` gehört **nicht** ins Git. Die Werte für `BRING_*` stehen bereits in der
bestehende Bring-Bridge, `YUVOMI_TOKEN` in `/opt/yuvomi/.env`.

`YUVOMI_URL=http://yuvomi:3000` (Docker-intern, Netz `web`) und `POLL_SECONDS`
stehen im Abschnitt `environment:` der Compose-Datei – dieser gewinnt über
`env_file`, also dort ändern und nicht in `.env`.

## Betrieb

```bash
docker logs -f yuvomi-bring                       # laufender Takt
docker compose run --rm yuvomi-bring python bridge.py --once --dry-run
docker restart yuvomi-bring                       # Katalog und Zustand neu einlesen
```

* Zustand in `./data/bridge.db` (Mapping `bring_uuid ↔ yuvomi_item_id` + letzter
  Stand pro Artikel). Ohne diese Datei paart die Brücke beide Seiten neu über den
  Namen – nichts geht verloren, es entstehen aber keine Duplikate.
* `./data/last_ok` ist der Healthcheck-Marker; älter als 60 s → Container `unhealthy`.
* Katalog wird einmal pro Start geladen, also nach Katalogänderung neu starten.

## Bekannte Grenzen

* Bring kennt kein `updated_at`: Konflikte werden nach dem Regelwerk oben
  entschieden, nicht nach Zeitstempel.
* Bring räumt „Zuletzt gekauft" selbst auf. Ein dort verschwundener, bereits
  gekaufter Artikel bleibt in Yuvomi stehen (Regel „nur offene Artikel löschen"),
  bis er dort gelöscht wird – z. B. über „Abgehakte leeren".
* Umbenennen eines Artikels, während er in „Zuletzt gekauft" steht, wird auf das
  Ent-haken vertagt (der ADD-Pfad der Library würde ihn sonst zurück auf die
  Einkaufsliste holen).
* Die Bring-API ist inoffiziell. Änderungen dort brechen die Brücke, nicht
  Yuvomi; die Paketversion ist in `requirements.txt` gepinnt.
