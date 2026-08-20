"""Nearest-gridpoint selection for NBM CONUS extracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GridPoint:
    lat: float
    lon: float
    row: int
    col: int
    distance_km: float


def _normalize_lon(lon: float) -> float:
    """Map longitude to [-180, 180) for consistent GRIB 0–360 vs -180–180 grids."""
    return ((lon + 180.0) % 360.0) - 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0
    lon1 = _normalize_lon(lon1)
    lon2 = _normalize_lon(lon2)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def nearest_on_mesh(
    lats: Any,
    lons: Any,
    *,
    target_lat: float,
    target_lon: float,
) -> GridPoint:
    """Select nearest lattice point by haversine distance.

    Supports 1D lat × 1D lon meshes and aligned 2D lat/lon coordinate arrays.
    """
    lat_arr = _as_array(lats)
    lon_arr = _as_array(lons)
    if lat_arr.ndim == 1 and lon_arr.ndim == 1:
        best_row = 0
        best_col = 0
        best_dist = float("inf")
        for row, lat in enumerate(lat_arr.data):
            for col, lon in enumerate(lon_arr.data):
                dist = haversine_km(target_lat, target_lon, lat, lon)
                if dist < best_dist:
                    best_dist = dist
                    best_row = row
                    best_col = col
        return GridPoint(
            lat=lat_arr.data[best_row],
            lon=lon_arr.data[best_col],
            row=best_row,
            col=best_col,
            distance_km=best_dist,
        )
    if lat_arr.ndim == 2 and lon_arr.ndim == 2:
        best_row = 0
        best_col = 0
        best_dist = float("inf")
        rows, cols = lat_arr.shape
        for row in range(rows):
            for col in range(cols):
                dist = haversine_km(
                    target_lat,
                    target_lon,
                    lat_arr.data[row][col],
                    lon_arr.data[row][col],
                )
                if dist < best_dist:
                    best_dist = dist
                    best_row = row
                    best_col = col
        return GridPoint(
            lat=lat_arr.data[best_row][best_col],
            lon=lon_arr.data[best_row][best_col],
            row=best_row,
            col=best_col,
            distance_km=best_dist,
        )
    raise ValueError(f"unsupported lat/lon shapes: {lat_arr.shape} {lon_arr.shape}")


def nearest_gridpoint(
    lats: list[float] | tuple[float, ...],
    lons: list[float] | tuple[float, ...],
    *,
    target_lat: float,
    target_lon: float,
) -> GridPoint:
    """Select nearest lattice point on a 1D lat × 1D lon mesh."""
    return nearest_on_mesh(lats, lons, target_lat=target_lat, target_lon=target_lon)


@dataclass
class _ArrayView:
    data: list[float] | list[list[float]]
    ndim: int
    shape: tuple[int, ...]


def _as_array(values: Any) -> _ArrayView:
    if hasattr(values, "ndim") and hasattr(values, "shape"):
        if values.ndim == 1:
            flat = [float(x) for x in values.flatten()]
            return _ArrayView(data=flat, ndim=1, shape=(len(flat),))
        if values.ndim == 2:
            rows = [[float(x) for x in row] for row in values]
            return _ArrayView(
                data=rows,
                ndim=2,
                shape=(len(rows), len(rows[0]) if rows else 0),
            )
    if isinstance(values, tuple):
        values = list(values)
    if isinstance(values, list):
        if values and isinstance(values[0], (list, tuple)):
            rows = [[float(x) for x in row] for row in values]
            return _ArrayView(
                data=rows,
                ndim=2,
                shape=(len(rows), len(rows[0]) if rows else 0),
            )
        flat = [float(x) for x in values]
        return _ArrayView(data=flat, ndim=1, shape=(len(flat),))
    raise TypeError(f"unsupported coordinate type: {type(values)!r}")
