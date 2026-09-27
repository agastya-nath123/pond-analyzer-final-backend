"""
Contour-map analysis for village pond planning.

Pipeline
    1. Read contour lines from a KML or KMZ file.
    2. Project them to the UTM zone the map lies in, so distances are metres.
    3. Sample every contour at 5 m spacing and interpolate a 5 m elevation
       grid (DEM) from the samples with linear interpolation.
    4. Fill depressions with a priority flood. Each connected flooded area
       is a "basin": ground that holds water until it overflows at its
       lowest rim point.
    5. A basin at least 1 m deep somewhere is a pond. For each pond the
       service reports the area 1 m deep or more, the greatest depth and
       the volume of water the basin holds when full.
    6. Flow directions: each cell drains to its steepest downhill neighbour
       (D8), and flat ground and lake surfaces drain to their nearest exit.
       This gives each pond's outlet and its catchment: every cell whose
       runoff reaches it, including ponds upstream that overflow into it.

The terrain for a file is cached, so /findCatchment after /analyzeContour
on the same file does not rebuild it.
"""

from __future__ import annotations

import hashlib
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.etree.ElementTree import ParseError

import numba
import numpy as np
from pyproj import Transformer
from scipy import ndimage
from scipy.interpolate import griddata
from scipy.spatial import QhullError

try:
    # Rejects XML entity tricks such as "billion laughs" in uploaded files.
    from defusedxml import DefusedXmlException as _UnsafeXml
    from defusedxml.ElementTree import fromstring as _parse_xml
except ImportError:  # pragma: no cover - defusedxml is optional
    from xml.etree.ElementTree import fromstring as _stdlib_fromstring

    class _UnsafeXml(Exception):
        pass

    def _parse_xml(data: bytes):
        if b"<!ENTITY" in data:
            raise _UnsafeXml("XML entities are not allowed")
        return _stdlib_fromstring(data)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

RESOLUTION_M = 5            # DEM cell size
SAMPLE_SPACING_M = 5        # distance between samples along each contour
MIN_POND_DEPTH_M = 1.0      # a basin must be this deep somewhere to be a pond
DEPTH_TOLERANCE_M = 1e-9    # allowance for rounding at that threshold

MAX_KML_BYTES = 200 * 1024 * 1024   # uncompressed KML size, including KMZ
MAX_DEM_CELLS = 6_000_000           # about 150 km² at 5 m

CACHE_ENTRIES = 6                   # terrains kept in memory
CACHE_MAX_CELLS = 2_000_000         # larger terrains are not cached

# /findCatchment has always reported the spill point in UTM zone 44N, and
# existing clients rely on that. The analysis itself runs in the map's own
# UTM zone; latitude and longitude are returned as well.
REPORT_EPSG = 32644

# D8 neighbour steps (row, col), in a fixed order so ties resolve the same way.
_STEPS = np.array(
    [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)],
    dtype=np.int64,
)

_sum_labels = getattr(ndimage, "sum_labels", ndimage.sum)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ContourMapError(ValueError):
    """The file can't be used as a contour map (HTTP 400)."""


class MapTooLargeError(ContourMapError):
    """The map covers more ground than the service grids at once (HTTP 413)."""


class PondNotFoundError(LookupError):
    """No pond with the requested id in this map (HTTP 404)."""


# ---------------------------------------------------------------------------
# Reading KML and KMZ
# ---------------------------------------------------------------------------

_PLAIN_NUMBER = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:m|metres|meters)?\s*$", re.I)
_ANY_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_ELEVATION_FIELD = re.compile(
    r"^(?:z|elev|elv|elevation|contour|height|alt|altitude|level)(?:_?(?:m|metres|meters))?$",
    re.I,
)


def _read_bytes(source) -> bytes:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    return Path(source).read_bytes()


def _unpack_kmz(data: bytes) -> bytes:
    """Return the KML text, taking it out of the archive if data is a KMZ."""
    if not data.startswith(b"PK\x03\x04"):
        if len(data) > MAX_KML_BYTES:
            raise MapTooLargeError("The KML file is larger than the server accepts.")
        return data

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ContourMapError("The KMZ file doesn't contain a .kml file.")
            # doc.kml is the main document by convention.
            name = "doc.kml" if "doc.kml" in names else min(names, key=lambda n: (n.count("/"), n))
            info = archive.getinfo(name)
            if info.file_size > MAX_KML_BYTES:
                raise MapTooLargeError("The KML inside this KMZ is larger than the server accepts.")
            with archive.open(info) as handle:
                kml = handle.read(MAX_KML_BYTES + 1)
    except zipfile.BadZipFile as error:
        raise ContourMapError("The KMZ file is damaged and can't be opened.") from error

    if len(kml) > MAX_KML_BYTES:
        raise MapTooLargeError("The KML inside this KMZ is larger than the server accepts.")
    return kml


def _local(tag) -> str:
    """Tag name without its namespace, so any KML namespace works."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_coordinates(text: str | None):
    """Parse a KML coordinates string into (lon/lat array, altitude array)."""
    if not text:
        return None
    tuples = re.sub(r"\s*,\s*", ",", text.strip()).split()
    if not tuples:
        return None

    try:
        dims = tuples[0].count(",") + 1
        if dims in (2, 3) and all(t.count(",") + 1 == dims for t in tuples):
            values = np.array(",".join(tuples).split(","), dtype=float).reshape(-1, dims)
        else:
            rows = []
            for t in tuples:
                parts = t.split(",")
                if len(parts) >= 2:
                    alt = float(parts[2]) if len(parts) > 2 and parts[2] else np.nan
                    rows.append((float(parts[0]), float(parts[1]), alt))
            if not rows:
                return None
            values = np.array(rows, dtype=float)
    except ValueError:
        return None

    lon, lat = values[:, 0], values[:, 1]
    ok = np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) <= 180) & (np.abs(lat) <= 90)
    if not ok.any():
        return None
    alt = values[ok, 2] if values.shape[1] > 2 else None
    return values[ok, :2], alt


def _placemark_elevation(placemark, altitudes):
    """
    Elevation of a contour placemark, looked for in this order:
    a numeric <name> ("270", "270 m"), an ExtendedData field such as ELEV or
    CONTOUR, a name with exactly one number in it ("Contour 270 m"), and
    finally the altitude of the coordinates if every point has the same one.
    """
    name_el = next((c for c in placemark if _local(c.tag) == "name"), None)
    name = (name_el.text or "").strip() if name_el is not None else ""

    match = _PLAIN_NUMBER.match(name)
    if match:
        return float(match.group(1))

    for el in placemark.iter():
        tag = _local(el.tag)
        if tag == "Data":
            key = el.get("name", "")
            value = next((c.text for c in el if _local(c.tag) == "value"), None)
        elif tag == "SimpleData":
            key, value = el.get("name", ""), el.text
        else:
            continue
        if value is not None and _ELEVATION_FIELD.match(key.strip()):
            try:
                return float(value.strip())
            except ValueError:
                pass

    numbers = _ANY_NUMBER.findall(name)
    if len(numbers) == 1:
        return float(numbers[0])

    if altitudes is not None and altitudes.size:
        alt = altitudes[np.isfinite(altitudes)]
        # 0 usually means "no altitude given" rather than sea level.
        if alt.size == altitudes.size and np.ptp(alt) < 1e-6 and alt[0] != 0:
            return float(alt[0])

    return None


def _parse_contours(kml: bytes):
    """Return a list of (elevation, lon/lat array) for every contour line."""
    try:
        root = _parse_xml(kml)
    except _UnsafeXml as error:
        raise ContourMapError("The file uses XML entities or a DTD, which aren't allowed.") from error
    except ParseError as error:
        raise ContourMapError(f"The file isn't valid KML: {error}") from error

    contours = []
    for placemark in root.iter():
        if _local(placemark.tag) != "Placemark":
            continue

        # Every LineString in the placemark, including inside MultiGeometry.
        # Polygons (such as a boundary outline) and points (labels) are not
        # contours and are skipped.
        lines = []
        for el in placemark.iter():
            if _local(el.tag) != "LineString":
                continue
            coords_el = next((c for c in el if _local(c.tag) == "coordinates"), None)
            parsed = _parse_coordinates(coords_el.text if coords_el is not None else None)
            if parsed is not None:
                lines.append(parsed)
        if not lines:
            continue

        altitudes = None
        if all(alt is not None for _, alt in lines):
            altitudes = np.concatenate([alt for _, alt in lines])

        elevation = _placemark_elevation(placemark, altitudes)
        if elevation is None or not np.isfinite(elevation):
            continue

        contours.extend((elevation, lonlat) for lonlat, _ in lines)

    if not contours:
        raise ContourMapError(
            "No contour lines with elevations were found. Each contour must be a "
            "LineString placemark with its elevation in the name (for example 270), "
            "in an ExtendedData field such as ELEV or CONTOUR, or as the altitude "
            "of its coordinates."
        )
    return contours


def parse_kml(file_path):
    """
    Contours as [{"elevation": float, "coordinates": [(lon, lat), ...]}].
    Kept for scripts written against the earlier version of this module.
    """
    contours = _parse_contours(_unpack_kmz(_read_bytes(file_path)))
    return [
        {"elevation": elevation, "coordinates": [tuple(p) for p in lonlat]}
        for elevation, lonlat in contours
    ]


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180) // 6) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=32)
def _transformer(source: int, target: int) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


def _sample_contours(lines_xy, elevations, spacing):
    """Points every `spacing` metres along each contour, starting at its first vertex."""
    xs, ys, zs = [], [], []
    for xy, elevation in zip(lines_xy, elevations):
        if len(xy) > 1:
            step = np.hypot(*np.diff(xy, axis=0).T)
            xy = xy[np.concatenate(([True], step > 0))]  # drop repeated points
            step = step[step > 0]

        if len(xy) == 1:  # a single point: keep it as a spot height
            xs.append(xy[:, 0])
            ys.append(xy[:, 1])
            zs.append(np.full(1, elevation))
            continue

        along = np.concatenate(([0.0], np.cumsum(step)))
        distances = np.arange(0.0, along[-1] + 1e-9, spacing)
        xs.append(np.interp(distances, along, xy[:, 0]))
        ys.append(np.interp(distances, along, xy[:, 1]))
        zs.append(np.full(distances.size, elevation))

    points = np.column_stack((np.concatenate(xs), np.concatenate(ys)))
    return points, np.concatenate(zs)


def _grid_dem(points, elevations, resolution):
    """Linear interpolation of the samples onto a grid; NaN outside their hull."""
    min_x, min_y = points.min(axis=0)
    max_x, max_y = points.max(axis=0)
    x = np.arange(min_x, max_x, resolution)
    y = np.arange(max_y, min_y, -resolution)

    if x.size * y.size > MAX_DEM_CELLS:
        area_km2 = (max_x - min_x) * (max_y - min_y) / 1e6
        limit_km2 = MAX_DEM_CELLS * resolution ** 2 / 1e6
        raise MapTooLargeError(
            f"The contour map covers about {area_km2:,.0f} km². The server analyses up to "
            f"{limit_km2:,.0f} km² at a time, so split the map or select a smaller area."
        )
    if x.size < 3 or y.size < 3:
        raise ContourMapError("The contour map covers too little ground to analyse.")
    if np.unique(elevations).size < 2:
        raise ContourMapError("All contours have the same elevation, so there is no slope to analyse.")

    try:
        dem = griddata(points, elevations, (x[None, :], y[:, None]), method="linear")
    except QhullError as error:
        raise ContourMapError(
            "The contour points lie along a single line, so no surface can be built from them."
        ) from error

    return float(x[0]), float(y[0]), dem


def _steepest_descent(filled, valid, resolution):
    """
    D8: each cell drains to the neighbour with the steepest drop on the
    filled surface. -1 where no neighbour is lower (flats and lake surfaces).
    """
    rows, cols = filled.shape
    padded = np.pad(np.where(valid, filled, np.inf), 1, constant_values=np.inf)
    best_slope = np.zeros(filled.shape)
    best = np.full(filled.shape, -1, dtype=np.int64)
    for k, (dr, dc) in enumerate(_STEPS):
        neighbour = padded[1 + dr:1 + dr + rows, 1 + dc:1 + dc + cols]
        distance = resolution * (np.sqrt(2.0) if dr and dc else 1.0)
        slope = (filled - neighbour) / distance
        better = valid & (slope > best_slope)
        best_slope[better] = slope[better]
        best[better] = k

    index = np.arange(rows * cols, dtype=np.int64).reshape(rows, cols)
    down = np.full(rows * cols, -1, dtype=np.int64)
    has = best >= 0
    k = best[has]
    down[index[has]] = index[has] + _STEPS[k, 0] * cols + _STEPS[k, 1]
    return down


@numba.njit
def _heap_push(keys, ticket, cells, size, key, t, cell):
    i = size
    keys[i] = key
    ticket[i] = t
    cells[i] = cell
    while i > 0:
        p = (i - 1) >> 1
        if keys[p] < keys[i] or (keys[p] == keys[i] and ticket[p] < ticket[i]):
            break
        keys[p], keys[i] = keys[i], keys[p]
        ticket[p], ticket[i] = ticket[i], ticket[p]
        cells[p], cells[i] = cells[i], cells[p]
        i = p
    return size + 1


@numba.njit
def _heap_pop(keys, ticket, cells, size):
    top = cells[0]
    size -= 1
    keys[0], ticket[0], cells[0] = keys[size], ticket[size], cells[size]
    i = 0
    while True:
        left = 2 * i + 1
        if left >= size:
            break
        child = left
        right = left + 1
        if right < size and (
            keys[right] < keys[left] or (keys[right] == keys[left] and ticket[right] < ticket[left])
        ):
            child = right
        if keys[i] < keys[child] or (keys[i] == keys[child] and ticket[i] < ticket[child]):
            break
        keys[child], keys[i] = keys[i], keys[child]
        ticket[child], ticket[i] = ticket[i], ticket[child]
        cells[child], cells[i] = cells[i], cells[child]
        i = child
    return top, size


@numba.njit
def _priority_flood(dem, valid, rows, cols, steps):
    """
    Priority-flood depression filling (Barnes et al., 2014).

    Water rises from the edge of the data inwards, always from the lowest
    open cell. A cell lower than the water reaching it is part of a
    depression and is filled to that level; the result is the surface water
    would have if every depression were full.
    """
    n = rows * cols
    filled = dem.copy()
    closed = np.zeros(n, dtype=np.bool_)
    keys = np.empty(n, dtype=np.float64)
    ticket = np.empty(n, dtype=np.int64)
    cells = np.empty(n, dtype=np.int64)
    size = 0
    pits = np.empty(n, dtype=np.int64)
    pit_head = 0
    pit_tail = 0
    count = 0

    # Start from every cell on the edge of the data: the grid border and
    # cells next to no-data. Water leaves the map there.
    for i in range(n):
        if not valid[i]:
            closed[i] = True
            continue
        r = i // cols
        c = i - r * cols
        for k in range(8):
            rr = r + steps[k, 0]
            cc = c + steps[k, 1]
            if rr < 0 or rr >= rows or cc < 0 or cc >= cols or not valid[rr * cols + cc]:
                closed[i] = True
                size = _heap_push(keys, ticket, cells, size, dem[i], count, i)
                count += 1
                break

    while size > 0 or pit_head < pit_tail:
        # Finish spreading across a depression before opening the next cell.
        if pit_head < pit_tail:
            i = pits[pit_head]
            pit_head += 1
        else:
            i, size = _heap_pop(keys, ticket, cells, size)
        r = i // cols
        c = i - r * cols
        for k in range(8):
            rr = r + steps[k, 0]
            cc = c + steps[k, 1]
            if rr < 0 or rr >= rows or cc < 0 or cc >= cols:
                continue
            j = rr * cols + cc
            if closed[j]:
                continue
            closed[j] = True
            if dem[j] <= filled[i]:
                filled[j] = filled[i]
                pits[pit_tail] = j
                pit_tail += 1
            else:
                size = _heap_push(keys, ticket, cells, size, dem[j], count, j)
                count += 1
    return filled


@numba.njit
def _route_flats(filled, valid, down, rows, cols, steps):
    """
    Give every cell on flat ground or a lake surface a direction.

    A breadth-first search spreads across cells of exactly the same level,
    starting from the flat's exits: cells of that level that do drain
    downhill, and cells on the edge of the data, which drain off the map.
    Each flat cell then drains towards its nearest exit. A lake with two
    overflow points at the same height splits between them, which keeps
    results stable when contours put many sills at exactly equal heights.
    Paths only go to cells reached earlier, so they can't loop.
    """
    n = rows * cols
    queue = np.empty(n, dtype=np.int64)
    head = 0
    tail = 0
    reached = np.zeros(n, dtype=np.bool_)

    for i in range(n):
        if not valid[i]:
            continue
        r = i // cols
        c = i - r * cols
        is_exit = down[i] >= 0
        if not is_exit:
            for k in range(8):
                rr = r + steps[k, 0]
                cc = c + steps[k, 1]
                if rr < 0 or rr >= rows or cc < 0 or cc >= cols or not valid[rr * cols + cc]:
                    is_exit = True
                    break
        if not is_exit:
            continue
        for k in range(8):
            rr = r + steps[k, 0]
            cc = c + steps[k, 1]
            if rr < 0 or rr >= rows or cc < 0 or cc >= cols:
                continue
            j = rr * cols + cc
            if valid[j] and down[j] < 0 and filled[j] == filled[i]:
                queue[tail] = i
                tail += 1
                reached[i] = True
                break

    while head < tail:
        i = queue[head]
        head += 1
        r = i // cols
        c = i - r * cols
        for k in range(8):
            rr = r + steps[k, 0]
            cc = c + steps[k, 1]
            if rr < 0 or rr >= rows or cc < 0 or cc >= cols:
                continue
            j = rr * cols + cc
            if valid[j] and not reached[j] and down[j] < 0 and filled[j] == filled[i]:
                down[j] = i
                reached[j] = True
                queue[tail] = j
                tail += 1
    return down


@numba.njit
def _accumulate(down, valid):
    """Number of cells (itself included) whose runoff passes through each cell."""
    n = down.size
    inflow = np.zeros(n, dtype=np.int64)
    for i in range(n):
        if down[i] >= 0:
            inflow[down[i]] += 1
    acc = np.zeros(n, dtype=np.float64)
    stack = np.empty(n, dtype=np.int64)
    top = 0
    for i in range(n):
        if valid[i]:
            acc[i] = 1.0
        if inflow[i] == 0:
            stack[top] = i
            top += 1
    while top > 0:
        top -= 1
        i = stack[top]
        j = down[i]
        if j >= 0:
            acc[j] += acc[i]
            inflow[j] -= 1
            if inflow[j] == 0:
                stack[top] = j
                top += 1
    return acc


@dataclass(frozen=True)
class _Pond:
    pond_id: int
    basin: int
    area_ha: float
    max_depth_m: float
    volume_m3: float
    level_m: float


@dataclass
class _Terrain:
    shape: tuple
    x0: float           # easting of column 0, in the map's UTM zone
    y0: float           # northing of row 0
    resolution: float
    epsg: int
    basins: np.ndarray  # basin label per cell (0 = not flooded)
    down: np.ndarray    # flat index of the D8 downstream cell, -1 for none
    acc: np.ndarray     # flow accumulation per cell (flat)
    ponds: list
    by_id: dict


def _build_terrain(data: bytes, resolution: float) -> _Terrain:
    contours = _parse_contours(_unpack_kmz(data))

    lonlat = np.concatenate([coords for _, coords in contours])
    epsg = _utm_epsg(float(lonlat[:, 0].mean()), float(lonlat[:, 1].mean()))
    x, y = _transformer(4326, epsg).transform(lonlat[:, 0], lonlat[:, 1])
    splits = np.cumsum([len(coords) for _, coords in contours])[:-1]
    lines_xy = np.split(np.column_stack((x, y)), splits)

    points, elevations = _sample_contours(
        lines_xy, [elevation for elevation, _ in contours], SAMPLE_SPACING_M
    )
    x0, y0, dem = _grid_dem(points, elevations, resolution)

    valid = ~np.isnan(dem)
    rows, cols = dem.shape
    dem = np.where(valid, dem, 0.0)  # values outside the data are never used
    valid_flat = valid.ravel()

    # Everything stays in memory: no shared dem.tif between requests.
    filled_flat = _priority_flood(dem.ravel(), valid_flat, rows, cols, _STEPS)
    filled = filled_flat.reshape(rows, cols)

    # Downhill cells follow the steepest drop; flat ground and lake surfaces
    # drain to their nearest exit. (pysheds' resolve_flats left some cells
    # with no direction, which stopped water inside lakes.)
    down = _steepest_descent(filled, valid, resolution)
    down = _route_flats(filled_flat, valid_flat, down, rows, cols, _STEPS)
    acc = _accumulate(down, valid_flat).astype(np.float32)

    # Cells the flood didn't raise have exactly their own elevation.
    fill_depth = np.where(valid, filled - dem, 0.0)
    basins, n_basins = ndimage.label(fill_depth > 0, structure=np.ones((3, 3), dtype=bool))
    basins = basins.astype(np.int32)

    ponds = []
    if n_basins:
        labels = list(range(1, n_basins + 1))
        deep = fill_depth >= MIN_POND_DEPTH_M - DEPTH_TOLERANCE_M
        deep_cells = _sum_labels(deep, basins, labels)
        volume = _sum_labels(fill_depth, basins, labels) * resolution ** 2
        max_depth = ndimage.maximum(fill_depth, basins, labels)
        level = ndimage.maximum(filled, basins, labels)

        # A basin can't be judged if it reaches the edge of the grid.
        edge = set(np.unique(np.concatenate((basins[0], basins[-1], basins[:, 0], basins[:, -1]))).tolist())

        candidates = [
            b for b in labels
            if deep_cells[b - 1] > 0 and b not in edge
        ]
        # Pond 1 stores the most water. Ties are broken so ids are stable.
        candidates.sort(key=lambda b: (-volume[b - 1], -deep_cells[b - 1], b))

        for rank, b in enumerate(candidates, start=1):
            ponds.append(_Pond(
                pond_id=rank,
                basin=int(b),
                area_ha=float(deep_cells[b - 1] * resolution ** 2 / 10_000),
                max_depth_m=float(max_depth[b - 1]),
                volume_m3=float(volume[b - 1]),
                level_m=float(level[b - 1]),
            ))

    return _Terrain(
        shape=(rows, cols),
        x0=x0,
        y0=y0,
        resolution=float(resolution),
        epsg=epsg,
        basins=basins,
        down=down.astype(np.int32) if down.size < 2**31 else down,
        acc=acc,
        ponds=ponds,
        by_id={p.pond_id: p for p in ponds},
    )


# ---------------------------------------------------------------------------
# Cache: one terrain build per file, shared by both endpoints
# ---------------------------------------------------------------------------

_cache: "OrderedDict[str, _Terrain]" = OrderedDict()
_building: dict = {}
_cache_lock = threading.Lock()


def _terrain(source, resolution) -> _Terrain:
    data = _read_bytes(source)
    key = f"{hashlib.sha256(data).hexdigest()}:{resolution}"

    with _cache_lock:
        terrain = _cache.get(key)
        if terrain is not None:
            _cache.move_to_end(key)
            return terrain
        pending = _building.get(key)
        owner = pending is None
        if owner:
            pending = _building[key] = threading.Event()

    if not owner:
        # Another request is already building this terrain; wait for it.
        pending.wait(timeout=600)
        with _cache_lock:
            terrain = _cache.get(key)
        return terrain if terrain is not None else _build_terrain(data, resolution)

    try:
        terrain = _build_terrain(data, resolution)
        if terrain.basins.size <= CACHE_MAX_CELLS:
            with _cache_lock:
                _cache[key] = terrain
                while len(_cache) > CACHE_ENTRIES:
                    _cache.popitem(last=False)
        return terrain
    finally:
        with _cache_lock:
            _building.pop(key, None)
        pending.set()


# ---------------------------------------------------------------------------
# Public functions used by the API routes
# ---------------------------------------------------------------------------

def analyze_ponds(kml_file, resolution=RESOLUTION_M):
    """
    Find ponds in a contour map (path, or the file's bytes).

    Returns a list sorted by storage, largest first:
        [{"pond_id", "pond_area_ha", "max_depth_m", "volume_m3"}, ...]
    """
    terrain = _terrain(kml_file, resolution)
    return [
        {
            "pond_id": p.pond_id,
            "pond_area_ha": p.area_ha,
            "max_depth_m": p.max_depth_m,
            "volume_m3": p.volume_m3,
        }
        for p in terrain.ponds
    ]


def _catchment(terrain: _Terrain, pond: _Pond):
    """
    Cells whose runoff reaches the pond, and the cell where it overflows.

    Every cell follows its D8 path downhill. The pond's cells are made
    terminal, then pointer jumping (each cell repeatedly adopting its
    target's target) finds where each path ends in about log2(path length)
    vectorised steps. Cells whose path ends in the pond form the catchment.
    """
    in_pond = terrain.basins.ravel() == pond.basin
    ids = np.arange(in_pond.size)

    target = terrain.down.astype(np.int64)
    stop = (target < 0) | in_pond
    target[stop] = ids[stop]
    for _ in range(64):
        jumped = target[target]
        if np.array_equal(jumped, target):
            break
        target = jumped
    draining_cells = int(np.count_nonzero(in_pond[target]))

    # Outlet: the pond cell through which the most water leaves it.
    pond_cells = np.flatnonzero(in_pond)
    downstream = terrain.down[pond_cells].astype(np.int64)
    leaving = pond_cells[(downstream >= 0) & ~in_pond[np.maximum(downstream, 0)]]
    candidates = leaving if leaving.size else pond_cells
    outlet = int(candidates[np.argmax(terrain.acc[candidates])])

    return draining_cells, outlet


def find_catchment(kml_file, pond_id, resolution=RESOLUTION_M):
    """
    Outlet and catchment of one pond from analyze_ponds on the same file.

    Returns:
        {
          "spill": {easting, northing, elevation_m, latitude, longitude},
          "flow_accumulation_cells", "catchment_area_m2",
          "catchment_area_ha", "catchment_pond_ratio"
        }
    easting/northing are in UTM zone 44N (EPSG:32644) as in earlier
    versions; elevation_m is the water level at which the pond overflows.
    """
    terrain = _terrain(kml_file, resolution)
    pond = terrain.by_id.get(int(pond_id))
    if pond is None:
        raise PondNotFoundError(
            f"Pond {pond_id} not found. This map has {len(terrain.ponds)} "
            f"pond{'s' if len(terrain.ponds) != 1 else ''}, numbered from 1."
        )

    cells, outlet = _catchment(terrain, pond)
    row, col = divmod(outlet, terrain.shape[1])
    x = terrain.x0 + col * terrain.resolution
    y = terrain.y0 - row * terrain.resolution
    lon, lat = _transformer(terrain.epsg, 4326).transform(x, y)
    easting, northing = _transformer(4326, REPORT_EPSG).transform(lon, lat)

    area_m2 = cells * terrain.resolution ** 2
    area_ha = area_m2 / 10_000

    return {
        "spill": {
            "easting": float(easting),
            "northing": float(northing),
            "elevation_m": pond.level_m,
            "latitude": float(lat),
            "longitude": float(lon),
        },
        "flow_accumulation_cells": cells,
        "catchment_area_m2": float(area_m2),
        "catchment_area_ha": float(area_ha),
        "catchment_pond_ratio": float(area_ha / pond.area_ha) if pond.area_ha > 0 else 0.0,
    }


def warm_up():
    """Compile the numba routines on a tiny grid so the first request is quick."""
    size = 24
    r, c = np.mgrid[0:size, 0:size]
    dem = (np.abs(r - size // 2) + np.abs(c - size // 2) + 100).astype(np.float64)
    dem[size // 2, size // 2] = 90
    valid = np.ones(dem.shape, dtype=bool)
    filled = _priority_flood(dem.ravel(), valid.ravel(), size, size, _STEPS)
    down = _steepest_descent(filled.reshape(dem.shape), valid, 5.0)
    down = _route_flats(filled, valid.ravel(), down, size, size, _STEPS)
    _accumulate(down, valid.ravel())
