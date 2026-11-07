"""Unit tests for the regional responder area helpers."""

from __future__ import annotations

import json

import pytest

from app.services.region_scope import (
    bboxes_to_region,
    parse_bbox,
    point_in_region,
    within_region,
)

SQUARE = json.dumps(
    {
        "type": "Polygon",
        "coordinates": [
            [[36.0, -2.0], [38.0, -2.0], [38.0, 0.0], [36.0, 0.0], [36.0, -2.0]]
        ],
    }
)


def test_point_inside_polygon():
    assert point_in_region(-1.0, 37.0, SQUARE)


def test_point_outside_polygon():
    assert not point_in_region(0.5, 37.0, SQUARE)
    assert not point_in_region(-1.0, 39.0, SQUARE)


def test_missing_point_fails_closed():
    assert not point_in_region(None, 37.0, SQUARE)
    assert not point_in_region(-1.0, None, SQUARE)


@pytest.mark.parametrize(
    "region",
    [
        "not json",
        "{}",
        json.dumps({"type": "Point", "coordinates": [37.0, -1.0]}),
        json.dumps({"type": "Polygon"}),
        json.dumps({"type": "Polygon", "coordinates": [[["a", "b"]]]}),
    ],
)
def test_unusable_region_fails_closed(region):
    assert not point_in_region(-1.0, 37.0, region)


def test_polygon_hole_is_excluded():
    with_hole = json.dumps(
        {
            "type": "Polygon",
            "coordinates": [
                [[36.0, -2.0], [38.0, -2.0], [38.0, 0.0], [36.0, 0.0], [36.0, -2.0]],
                [[36.5, -1.5], [37.5, -1.5], [37.5, -0.5], [36.5, -0.5], [36.5, -1.5]],
            ],
        }
    )
    assert not point_in_region(-1.0, 37.0, with_hole)
    assert point_in_region(-1.8, 36.2, with_hole)


def test_feature_wrapper_accepted():
    feature = json.dumps({"type": "Feature", "geometry": json.loads(SQUARE)})
    assert point_in_region(-1.0, 37.0, feature)


def test_bboxes_to_region_covers_each_settlement():
    kibera = parse_bbox("-1.3256,36.7647,-1.2993,36.8066")
    mathare = parse_bbox("-1.2674,36.8389,-1.2495,36.8790")
    mukuru = parse_bbox("-1.3300,36.8740,-1.3080,36.9000")
    region = bboxes_to_region([kibera, mathare, mukuru])

    assert json.loads(region)["type"] == "MultiPolygon"
    assert point_in_region(-1.3127, 36.7860, region)  # Kibera
    assert point_in_region(-1.2600, 36.8590, region)  # Mathare
    assert point_in_region(-1.3190, 36.8870, region)  # Mukuru kwa Njenga
    assert not point_in_region(-0.4197, 36.9476, region)  # Nyeri
    assert not point_in_region(-1.2490, 36.8740, region)  # Kariobangi South
    assert not point_in_region(-1.2550, 36.6780, region)  # Kinoo


@pytest.mark.parametrize(
    "value",
    [
        "1,2,3",
        "a,b,c,d",
        "-1.29,36.76,-1.32,36.80",  # min_lat > max_lat
        "-1.32,36.80,-1.29,36.76",  # min_lng > max_lng
        "-91,36,-1,37",
        "-1,-181,0,37",
    ],
)
def test_parse_bbox_rejects_bad_boxes(value):
    with pytest.raises(ValueError):
        parse_bbox(value)


def test_bboxes_to_region_needs_a_box():
    with pytest.raises(ValueError):
        bboxes_to_region([])


def test_within_region_binds_geojson():
    clause = within_region(SQUARE)
    assert "ST_Within" in str(clause)
    assert clause.compile().params["region_geojson"] == SQUARE
