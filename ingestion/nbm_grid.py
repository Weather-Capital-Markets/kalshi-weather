"""Nearest-gridpoint selection for NBM CONUS extracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


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


def _haversine_km_numpy(
    target_lat: float,
    target_lon: float,
    lat2: np.ndarray,
    lon2: np.ndarray,
) -> np.ndarray:
    radius_km = 6371.0
    lon1 = _normalize_lon(target_lon)
    lon2n = ((lon2 + 180.0) % 360.0) - 180.0
    phi1 = math.radians(target_lat)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - target_lat)
    dlambda = np.radians(lon2n - lon1)
    a = np.sin(dphi / 2.0) ** 2 + math.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_km * np.arcsin(np.sqrt(a))


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
    lat_arr = np.asarray(lats, dtype=float)
    lon_arr = np.asarray(lons, dtype=float)
    if lat_arr.ndim == 1 and lon_arr.ndim == 1:
        lat_grid, lon_grid = np.meshgrid(lat_arr, lon_arr, indexing="ij")
        dist = _haversine_km_numpy(target_lat, target_lon, lat_grid, lon_grid)
        flat = int(np.argmin(dist))
        row, col = np.unravel_index(flat, dist.shape)
        row_i, col_i = int(row), int(col)
        return GridPoint(
            lat=float(lat_arr[row_i]),
            lon=float(lon_arr[col_i]),
            row=row_i,
            col=col_i,
            distance_km=float(dist[row_i, col_i]),
        )
    if lat_arr.ndim == 2 and lon_arr.ndim == 2:
        dist = _haversine_km_numpy(target_lat, target_lon, lat_arr, lon_arr)
        flat = int(np.argmin(dist))
        row, col = np.unravel_index(flat, dist.shape)
        row_i, col_i = int(row), int(col)
        return GridPoint(
            lat=float(lat_arr[row_i, col_i]),
            lon=float(lon_arr[row_i, col_i]),
            row=row_i,
            col=col_i,
            distance_km=float(dist[row_i, col_i]),
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
