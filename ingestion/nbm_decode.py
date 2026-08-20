"""Decode NBM grib message bytes to percentile ladders in Fahrenheit."""

from __future__ import annotations

import io
import logging
from typing import Any

from ingestion.nbm_grid import nearest_on_mesh
from ingestion.nbm_idx import kelvin_to_fahrenheit

logger = logging.getLogger(__name__)


def decode_percentile_value_f(grib_bytes: bytes) -> float | None:
    """Decode a single grib2 message and return scalar TMP in Fahrenheit."""
    try:
        import cfgrib  # type: ignore[import-untyped]
    except ImportError:
        logger.error("cfgrib is required for NBM decode; install requirements-analysis.txt")
        return None
    try:
        datasets = cfgrib.open_datasets(io.BytesIO(grib_bytes))
    except Exception as exc:
        logger.warning("cfgrib decode failed: %s", exc)
        return None
    if not datasets:
        return None
    ds = datasets[0]
    var = None
    for name in ("t2m", "2t", "TMP", "t"):
        if name in ds:
            var = name
            break
    if var is None:
        data_vars = list(ds.data_vars)
        if not data_vars:
            return None
        var = data_vars[0]
    value = float(ds[var].values.flatten()[0])
    return kelvin_to_fahrenheit(value)


def _coord_names(ds: Any) -> tuple[str, str] | None:
    lat_name = "latitude" if "latitude" in ds.coords else "lat"
    lon_name = "longitude" if "longitude" in ds.coords else "lon"
    if lat_name not in ds.coords or lon_name not in ds.coords:
        return None
    return lat_name, lon_name


def extract_grid_lats_lons(grib_bytes: bytes) -> tuple[list[float], list[float]] | None:
    try:
        import cfgrib  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        datasets = cfgrib.open_datasets(io.BytesIO(grib_bytes))
    except Exception:
        return None
    if not datasets:
        return None
    ds = datasets[0]
    names = _coord_names(ds)
    if names is None:
        return None
    lat_name, lon_name = names
    lat_vals = ds[lat_name].values
    lon_vals = ds[lon_name].values
    if lat_vals.ndim == 2 and lon_vals.ndim == 2:
        lats = [float(x) for x in lat_vals[:, 0]]
        lons = [float(x) for x in lon_vals[0, :]]
        return lats, lons
    if lat_vals.ndim == 1 and lon_vals.ndim == 1:
        return [float(x) for x in lat_vals], [float(x) for x in lon_vals]
    return None


def nearest_gridpoint_from_grib(
    grib_bytes: bytes,
    *,
    target_lat: float,
    target_lon: float,
) -> tuple[int, int, float, float, float] | None:
    """Return (row, col, grid_lat, grid_lon, distance_km) for a grib message."""
    try:
        import cfgrib  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        datasets = cfgrib.open_datasets(io.BytesIO(grib_bytes))
    except Exception:
        return None
    if not datasets:
        return None
    ds = datasets[0]
    names = _coord_names(ds)
    if names is None:
        return None
    lat_name, lon_name = names
    point = nearest_on_mesh(
        ds[lat_name].values,
        ds[lon_name].values,
        target_lat=target_lat,
        target_lon=target_lon,
    )
    return point.row, point.col, point.lat, point.lon, point.distance_km


def decode_message_at_gridpoint(
    grib_bytes: bytes,
    *,
    row: int,
    col: int,
) -> float | None:
    try:
        import cfgrib  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        datasets = cfgrib.open_datasets(io.BytesIO(grib_bytes))
    except Exception:
        return None
    if not datasets:
        return None
    ds = datasets[0]
    var = next(iter(ds.data_vars))
    values = ds[var].values
    if values.ndim == 2:
        return kelvin_to_fahrenheit(float(values[row, col]))
    return kelvin_to_fahrenheit(float(values.flatten()[0]))
