"""Synthetic crisis-scenario generation with ground-truth labels.

Deterministic given ``seed`` so a run reproduces exactly. ``generate()``
returns a list of ``PlannedReport`` in submission order; each carries the
request payload and the ground truth the collector grades against
(duplicate-cluster membership and the canonical member of that cluster).

Geography
---------
Reports sit on synthetic building footprints laid out on a grid (see
``footprints.py``). ``dup_fraction`` of reports are deliberate duplicates:
same footprint, GPS jittered a few metres, same crisis/infrastructure
category, and an image that is a lightly-perturbed copy of the cluster's
canonical image. The rest are distinct incidents on their own footprints
with independently generated images. A small fraction have no GPS (landmark
text only) and a small fraction have no photo.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

CRISIS_TYPES = ["flood", "earthquake", "conflict", "wildfire", "other"]
INFRA_TYPES = [
    "residential", "commercial", "government", "utilities", "transport", "community",
]
SEVERITIES = ["minimal", "partial", "destroyed"]
ELEC = ["functional", "non_functional", "unknown"]
HEALTH = ["accessible", "inaccessible", "unknown"]
LANGS = ["en", "sw", "fr", "ar"]

# Realistic "most pressing needs" strings. One deliberately contains a name +
# phone number to demonstrate the export free-text PII gap (audit finding M-5).
NEEDS = [
    "water and shelter for 6 families",
    "medical help, several injured",
    "food and blankets urgently",
    "road access blocked by debris",
    "call John Otieno on +254712345678, he has the keys",  # PII probe
    "",
]


@dataclass
class PlannedReport:
    idx: int                              # submission order, 0-based
    identity: int                         # stable id used to resolve clusters
    crisis_type: str
    infrastructure_type: str
    damage_severity: str
    lat: Optional[float]
    lng: Optional[float]
    gps_accuracy_m: Optional[float]
    landmark_description: Optional[str]
    electricity_status: Optional[str]
    health_services_status: Optional[str]
    most_pressing_needs: Optional[str]
    debris_clearing_needed: bool
    lang: str
    has_photo: bool
    image_bytes: Optional[bytes]
    # ── ground truth ──────────────────────────────────────────────────────────
    building_external_id: Optional[str]   # seeded footprint it sits on (or None)
    dup_cluster: Optional[int]            # None => distinct incident
    is_canonical: bool                    # first member of its cluster
    canonical_identity: Optional[int]     # identity of the cluster's canonical


def _canonical_image(rng: random.Random) -> np.ndarray:
    h = w = 256
    base = np.zeros((h, w, 3), dtype=np.uint8)
    r0, g0, b0 = rng.randint(20, 200), rng.randint(20, 200), rng.randint(20, 200)
    for by in range(4):
        for bx in range(4):
            base[by * 64:(by + 1) * 64, bx * 64:(bx + 1) * 64] = (
                (r0 + bx * 17 + by * 5) % 256,
                (g0 + by * 19 + bx * 7) % 256,
                (b0 + (bx + by) * 13) % 256,
            )
    for i in range(h):
        j = int(i * 0.7)
        if j < w:
            base[i, max(0, j - 3):j + 3] = (240, 240, 240)
    return base


def _to_jpeg(arr: np.ndarray, quality: int = 85) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _perturb(arr: np.ndarray, rng: random.Random, strength: int = 6) -> np.ndarray:
    out = np.clip(arr.astype(np.int16) + rng.randint(-strength, strength), 0, 255)
    out = out.astype(np.uint8)
    if rng.randint(0, 2):
        out = np.roll(out, rng.randint(1, 2), axis=0)
    return out


def generate(
    *,
    n: int,
    seed: int,
    footprint_ids: list[str],
    footprint_points: dict[str, tuple[float, float]],
    photo_fraction: float = 0.85,
    dup_fraction: float = 0.25,
    avg_cluster_size: float = 3.0,
    no_gps_fraction: float = 0.06,
    shuffle: bool = True,
) -> list[PlannedReport]:
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    if len(footprint_ids) < n:
        raise ValueError(
            f"need at least {n} seeded footprints, have {len(footprint_ids)}"
        )
    fps = list(footprint_ids)
    rng.shuffle(fps)

    n_dup = int(n * dup_fraction)
    # Partition the first n_dup identities into clusters of >=2.
    clusters: list[list[int]] = []
    i = 0
    while i < n_dup:
        size = max(2, int(np_rng.poisson(max(0.1, avg_cluster_size - 1)) + 2))
        size = min(size, n_dup - i)
        if size < 2:                       # tail remainder -> fold into last
            if clusters:
                clusters[-1].extend(range(i, n_dup))
            i = n_dup
            break
        clusters.append(list(range(i, i + size)))
        i += size

    identity_cluster: dict[int, int] = {}
    identity_canon: dict[int, int] = {}
    for k, cl in enumerate(clusters):
        for ident in cl:
            identity_cluster[ident] = k
            identity_canon[ident] = cl[0]

    specs: list[PlannedReport] = []
    canon_img: dict[int, np.ndarray] = {}
    fp_ptr = 0

    for ident in range(n):
        has_photo = rng.random() < photo_fraction
        lang = rng.choice(LANGS)
        needs = rng.choice(NEEDS)
        debris = rng.random() < 0.4
        elec = rng.choice(ELEC)
        health = rng.choice(HEALTH)

        if ident in identity_cluster:
            k = identity_cluster[ident]
            canon = identity_canon[ident]
            ext_id = fps[k % len(fps)]              # one footprint per cluster
            blat, blng = footprint_points[ext_id]
            crisis = CRISIS_TYPES[k % len(CRISIS_TYPES)]
            infra = INFRA_TYPES[k % len(INFRA_TYPES)]
            sev = SEVERITIES[k % len(SEVERITIES)]
            lat = blat + rng.uniform(-7e-5, 7e-5)
            lng = blng + rng.uniform(-7e-5, 7e-5)
            acc = rng.uniform(4, 12)
            if canon not in canon_img:
                canon_img[canon] = _canonical_image(rng)
            img = (
                canon_img[canon] if ident == canon
                else _perturb(canon_img[canon], rng)
            )
            specs.append(PlannedReport(
                idx=-1, identity=ident, crisis_type=crisis,
                infrastructure_type=infra, damage_severity=sev,
                lat=lat, lng=lng, gps_accuracy_m=acc, landmark_description=None,
                electricity_status=elec, health_services_status=health,
                most_pressing_needs=needs, debris_clearing_needed=debris,
                lang=lang, has_photo=has_photo,
                image_bytes=_to_jpeg(img) if has_photo else None,
                building_external_id=ext_id, dup_cluster=k,
                is_canonical=(ident == canon), canonical_identity=canon,
            ))
        else:
            ext_id = fps[len(clusters) + fp_ptr]
            fp_ptr += 1
            blat, blng = footprint_points[ext_id]
            no_gps = rng.random() < no_gps_fraction
            if no_gps:
                lat = lng = acc = None
                landmark = f"near seeded structure {ext_id}"
                gt_fp = None
            else:
                lat = blat + rng.uniform(-5e-5, 5e-5)
                lng = blng + rng.uniform(-5e-5, 5e-5)
                acc = rng.uniform(4, 40)
                landmark = None
                gt_fp = ext_id
            specs.append(PlannedReport(
                idx=-1, identity=ident, crisis_type=rng.choice(CRISIS_TYPES),
                infrastructure_type=rng.choice(INFRA_TYPES),
                damage_severity=rng.choice(SEVERITIES),
                lat=lat, lng=lng, gps_accuracy_m=acc,
                landmark_description=landmark, electricity_status=elec,
                health_services_status=health, most_pressing_needs=needs,
                debris_clearing_needed=debris, lang=lang, has_photo=has_photo,
                image_bytes=_to_jpeg(_canonical_image(rng)) if has_photo else None,
                building_external_id=gt_fp, dup_cluster=None,
                is_canonical=False, canonical_identity=None,
            ))

    if shuffle:
        # Keep each cluster's canonical member ahead of its other members so
        # "duplicate of an earlier report" is achievable, but otherwise
        # randomise arrival order.
        rng.shuffle(specs)
        # Ensure each cluster's canonical member precedes its other members.
        pos = {s.identity: i for i, s in enumerate(specs)}
        for k, cl in enumerate(clusters):
            canon = cl[0]
            cpos = pos[canon]
            for ident in cl[1:]:
                if pos[ident] < cpos:
                    specs[pos[ident]], specs[cpos] = specs[cpos], specs[pos[ident]]
                    pos[canon], pos[ident] = pos[ident], pos[canon]
                    cpos = pos[canon]

    for i, s in enumerate(specs):
        s.idx = i
    return specs
