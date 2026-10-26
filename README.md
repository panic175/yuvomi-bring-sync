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
| gelöscht | nach zweimal bestätigtem Fehlen, **auch abgehakt** (Bring entscheidet) | `DELETE` → `remove_item` |
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

## Tests

```bash
python3 -m unittest -v test_bridge
```

83 Tests, offline und ohne Fremdpakete – weder `aiohttp` noch `bring-api`
werden gebraucht, die Suite läuft damit überall (lokal, im CI, im Container).
Abgedeckt sind der Planner, die SQLite-Persistenz, der Yuvomi-Client, der
Produktkatalog, das Paaren in `reconcile()`, die Aktionsausführung und die
Orchestrierung inklusive 429- und Fehlerpfade.

Abdeckung von `bridge.py`: **93 %** der Anweisungen (Stand 25.10.2026). Es
fehlt bewusst nur noch
`run()`/`main()` – der Einstiegspunkt braucht `aiohttp`, `bring-api` und Netz;
er wird im Container über `--once` und `--dry-run` geprüft. Messung:

```bash
pip install coverage
coverage run --source=bridge -m unittest test_bridge && coverage report -m
```

## Bekannte Grenzen

* Bring kennt kein `updated_at`: Konflikte werden nach dem Regelwerk oben
  entschieden, nicht nach Zeitstempel.
* **Fehlen heißt Löschen** (Entscheidung 25.10.2026): Bring entscheidet, was
  existiert. Verschwindet ein Artikel dort, ist er nach zwei Takt auch in
  Yuvomi weg – abgehakt oder nicht. Wer also in Bring „Zuletzt gekauft"
  leert, räumt auch Yuvomi auf. Ausgenommen bleibt ein eigener Push, der nie
  ankam: der wird bis zu `MAX_PUSH_ATTEMPTS` erneut versucht und lebt sonst
  allein in Yuvomi (Löschen wäre Datenverlust, den wir selbst verursacht haben).
* Bring vergibt demselben Artikel manchmal eine neue UUID (Löschen und Neu-
  anlegen). Die alte Zeile wird dann nach zwei Takten gelöscht, bis dahin
  steht der Artikel kurzzeitig doppelt in Yuvomi.
* Umbenennen eines Artikels, während er in „Zuletzt gekauft" steht, wird auf das
  Ent-haken vertagt (der ADD-Pfad der Library würde ihn sonst zurück auf die
  Einkaufsliste holen).
* Die Bring-API ist inoffiziell. Änderungen dort brechen die Brücke, nicht
  Yuvomi; die Paketversion ist in `requirements.txt` gepinnt.
