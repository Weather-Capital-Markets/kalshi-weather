"""Tests for NBM grid nearest-point selection."""

from __future__ import annotations

import pytest

from ingestion.nbm_grid import nearest_gridpoint, nearest_on_mesh


def test_nearest_on_1d_mesh() -> None:
    lats = [40.0, 41.0]
    lons = [-74.0, -73.0]
    point = nearest_on_mesh(lats, lons, target_lat=40.5, target_lon=-73.5)
    assert point.row == 1
    assert point.col == 0
    assert point.lat == 41.0
    assert point.lon == -74.0


def test_nearest_on_2d_coordinates() -> None:
    lats = [
        [40.0, 40.0],
        [41.0, 41.0],
    ]
    lons = [
        [-74.0, -73.0],
        [-74.0, -73.0],
    ]
    point = nearest_on_mesh(lats, lons, target_lat=40.9, target_lon=-73.1)
    assert point.row == 1
    assert point.col == 1


def test_nearest_gridpoint_alias() -> None:
    point = nearest_gridpoint([40.0], [-73.0], target_lat=40.0, target_lon=-73.0)
    assert point.distance_km == 0.0


def test_haversine_normalizes_360_degree_longitudes() -> None:
    from ingestion.nbm_grid import haversine_km

    direct = haversine_km(40.779, -73.969, 40.780, -73.970)
    wrapped = haversine_km(40.779, -73.969, 40.780, 286.030)
    assert direct == pytest.approx(wrapped, rel=1e-9)
