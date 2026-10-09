# County deployments

Stand up a county's own Matata site, with reporting app, analyst portal and API, at
`https://<slug>.<BASE_DOMAIN>` with one command. This is the operator side of
the 48-hour county setup promise.

```
deploy/
├── provision-county.sh     # the one command
├── operator.env.example    # settings shared by every county on a server
├── shared/                 # once per server: Caddy (HTTPS), Ollama, LibreTranslate
└── county/                 # one stack per county: API, workers, Postgres, Redis, MinIO, web
```

Each county gets its own Postgres, Redis and MinIO volumes, its own secrets and
its own frontend build, so no report, photo or account is ever shared between
counties. The heavy, stateless services (HTTPS proxy, vision model, translation)
run once per server and every county uses them.

## One-time server setup

1. A Linux server with Docker and Compose v2, ports 80 and 443 open.
   Sizing: about 2 GB RAM per county, plus 6 GB if `VISION_PROVIDER=ollama`
   (the shared LLaVA model) and 2 GB if `TRANSLATION_PROVIDER=libretranslate`.
2. DNS: a wildcard `*.<BASE_DOMAIN>` A record pointing at the server, or one
   record per county.
3. Clone both repos side by side:
   ```bash
   git clone https://github.com/Kubu-Ventures/Matata-backend
   git clone https://github.com/Kubu-Ventures/Matata
   ```
4. `cp deploy/operator.env.example deploy/operator.env` and fill it in:
   Privy app ID and key, AWS keys for Rekognition photo screening,
   `FRONTEND_DIR` pointing at `Matata/matata-app`.

## Provision a county

```bash
deploy/provision-county.sh --slug tana-river --name "Tana River County" \
    --admin coordinator@example.go.ke \
    --analyst drm1@example.go.ke --analyst gis@example.go.ke \
    --responder ops@example.go.ke \
    --hotspot "Hola=39.98,-1.53,40.06,-1.46" \
    --hotspot "Garsen=40.08,-2.30,40.15,-2.24"
```

What it does, in order:

1. Checks the server, DNS and `operator.env`.
2. Starts the shared services if they are not already running.
3. Generates the county's secrets once (`deploy/counties/<slug>/secrets.env`)
   and renders its settings.
4. Adds an HTTPS route for `<slug>.<BASE_DOMAIN>` to Caddy.
5. Builds and starts the county stack. The API runs database migrations.
6. Downloads OpenStreetMap buildings for each `--hotspot` from Overpass and
   imports them, so reports snap to real buildings from the first day.
7. Creates the review team's accounts (admin, analysts, responders).
8. Reloads Caddy, waits for the certificate and checks the site answers.

Hotspot boxes are `min_lng,min_lat,max_lng,max_lat`. Draw one at
[bboxfinder.com](http://bboxfinder.com) and copy the box. Keep each hotspot to
a few square kilometres, because the public Overpass server limits large queries. For a
whole county, download from the [HOT Export Tool](https://export.hotosm.org)
and pass `--footprints file.geojson` instead.

**Then, by hand:** add `https://<slug>.<BASE_DOMAIN>` to the Privy app's allowed
origins (Privy dashboard → Configuration → App settings). Until you do, nobody
on the review team can log in. Residents can report regardless.

## Day-to-day

Re-run the same command to add people (`--analyst new@…`) or hotspots. Secrets,
data and existing accounts are kept; images rebuild only if the code changed.
Pull new code in both repos first to ship an update.

| Task | Command |
| --- | --- |
| Logs | `docker compose -p matata-<slug> -f deploy/county/docker-compose.yml --env-file deploy/counties/<slug>/county.env logs -f app` |
| List accounts | `… exec app python -m app.cli list-accounts` |
| Remove someone | `… exec app python -m app.cli deactivate-account --id <uuid>` |
| Back up the database | `… exec -T postgres pg_dump -U matata -d matata_db -Fc > <slug>-$(date +%F).dump` |

Back up `deploy/counties/<slug>/secrets.env` off the server. Losing
`PHONE_HASH_SALT` orphans every account, and losing `POSTGRES_PASSWORD` locks
you out of the database.

## Testing locally

Set `BASE_DOMAIN=localhost` in `operator.env`. Caddy then issues
`https://<slug>.localhost` from its own internal CA, with no DNS or Let's Encrypt needed.
Use `VISION_PROVIDER=mock`, `TRANSLATION_PROVIDER=mock` and
`--no-photo-screening` to fit on a laptop.
