"""Rewrite .env for a docker-compose simulation run.

Non-destructive: only the keys below are changed; everything else (secrets,
etc.) is preserved. Original is backed up to .env.pre-sim once.
"""
import pathlib
import shutil

OVERRIDES = {
    # match docker-compose.yml postgres service (POSTGRES_USER/PASSWORD/DB = matata)
    "DATABASE_URL": "postgresql+asyncpg://matata:matata@postgres:5432/matata_db",
    "REDIS_URL": "redis://redis:6379",
    "CELERY_BROKER_URL": "redis://redis:6379/0",
    "CELERY_RESULT_BACKEND": "redis://redis:6379/1",
    "STORAGE_BACKEND": "s3",
    "S3_ENDPOINT_URL": "http://minio:9000",
    "S3_BUCKET_NAME": "crisismap",
    "MINIO_ROOT_USER": "crisismap_admin",
    "MINIO_ROOT_PASSWORD": "minioadmin",
    "MODERATION_PROVIDER": "sim",
    "VISION_PROVIDER": "sim",
    "GEOCODING_PROVIDER": "mock",
    "TRANSLATION_PROVIDER": "mock",
    "ENVIRONMENT": "development",
}

p = pathlib.Path(".env")
bak = pathlib.Path(".env.pre-sim")
if not bak.exists():
    shutil.copy(p, bak)
    print(f"backed up -> {bak}")

lines = p.read_text().splitlines()
seen = set()
out = []
for ln in lines:
    if "=" in ln and not ln.lstrip().startswith("#"):
        key = ln.split("=", 1)[0].strip()
        if key in OVERRIDES:
            out.append(f"{key}={OVERRIDES[key]}")
            seen.add(key)
            continue
    out.append(ln)
for key, val in OVERRIDES.items():
    if key not in seen:
        out.append(f"{key}={val}")
p.write_text("\n".join(out) + "\n")
print("applied:", ", ".join(sorted(OVERRIDES)))
