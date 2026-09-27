# Pond Planning API (backend)

A FastAPI service that finds where ponds fit in a contour map. Upload a KML
or KMZ file of contour lines and it returns every natural depression at
least 1 m deep, with its area, depth and storage, and for any one of them the
outlet where it overflows and the catchment that drains into it.

It is the backend for the `pond-frontend` app, and can be used on its own.

## Requirements

- Python 3.12 (tested with 3.12.3)
- The packages in `requirements.txt`, pinned to the versions tested
  together

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

From this folder (the one that contains `app/`):

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

- Interactive API docs: http://127.0.0.1:8000/docs
- No `uploads/` folder is needed; nothing is written to disk.
- At start-up the analysis code is compiled in the background (about
  1.6 s), so the first request doesn't wait for it.
- `--workers` runs several processes so several users can be served at
  once. Each worker keeps its own cache (see below).

## API

All analysis requests are `multipart/form-data` with the contour file in a
field named `contour_map`.

| Method and path | Form fields | Returns |
| --- | --- | --- |
| `GET /` | | `{"message": "Pond Planning API is running"}` |
| `POST /analyzeContour` | `contour_map` | Candidate ponds, largest storage first |
| `POST /findCatchment` | `contour_map`, `pond_id` | Outlet and catchment of one pond |

### Find ponds

```bash
curl -F "contour_map=@contours_1m.kml" http://127.0.0.1:8000/analyzeContour
```

```json
{
  "ponds": [
    { "pond_id": 1, "pond_area_ha": 21.74, "max_depth_m": 7.0, "volume_m3": 606865.0 },
    { "pond_id": 2, "pond_area_ha": 8.40, "max_depth_m": 12.0, "volume_m3": 282124.0 }
  ]
}
```

- `pond_id`: 1 is the pond that stores the most water. Ids are only
  meaningful for the same file.
- `pond_area_ha`: area 1 m deep or more.
- `max_depth_m`: greatest depth when full.
- `volume_m3`: water the whole basin holds when full, shallow edge
  included.

### Outlet and catchment of one pond

Send the same file with a `pond_id` from `/analyzeContour`:

```bash
curl -F "contour_map=@contours_1m.kml" -F "pond_id=1" http://127.0.0.1:8000/findCatchment
```

```json
{
  "pond_id": 1,
  "spill": {
    "easting": 529754.47,
    "northing": 2348920.58,
    "elevation_m": 274.0,
    "latitude": 21.24166,
    "longitude": 81.28676
  },
  "flow_accumulation_cells": 155365,
  "catchment_area_m2": 3884125.0,
  "catchment_area_ha": 388.41,
  "catchment_pond_ratio": 17.87
}
```

- `spill` is the outlet, where the pond overflows. `elevation_m` is the
  water level at which that happens.
- `easting`/`northing` are always in UTM zone 44N (EPSG:32644), as in
  version 1.0, whatever zone the map is in. `latitude`/`longitude` give the
  same point in WGS84.
- The catchment is every 5 m cell whose runoff reaches the pond, including
  ponds upstream that overflow into it. `catchment_pond_ratio` divides it
  by `pond_area_ha`.

### Errors

Every error has a readable `detail` message.

| Status | Meaning |
| --- | --- |
| 400 | Not a usable contour map: not XML, damaged KMZ, no contours with elevations, all contours at one elevation, XML entities, empty file |
| 404 | No pond with that `pond_id` in this file |
| 413 | The file (over 100 MB) or the ground it covers (over about 150 km²) is too large |
| 500 | Unexpected failure; the traceback is in the server log |

## Input format

KML or KMZ (for a KMZ, `doc.kml` or the first `.kml` inside is used). Each
contour is a `LineString` in a `Placemark`. MultiGeometry is fine and every
line in it is read. Any KML namespace, or none, is accepted.

The elevation of each contour is taken from the first of these that is
present:

1. a numeric `<name>`: `270`, `270.0` or `270 m`;
2. an ExtendedData field named like `ELEV`, `ELEVATION`, `CONTOUR`,
   `HEIGHT`, `ALT`, `LEVEL` or `Z` (`<Data>` or `<SimpleData>`);
3. a name with exactly one number in it, such as `Contour 270 m`;
4. the altitude of the coordinates, if it is the same non-zero value for
   every point.

Polygons (such as a boundary outline) and points (such as labels) are
ignored. A one-point line is used as a spot height.

```xml
<Placemark>
  <name>270</name>
  <LineString><coordinates>81.2901,21.2405 81.2907,21.2411 81.2915,21.2413</coordinates></LineString>
</Placemark>
```

## How it works

1. Parse the contours and project them to the UTM zone of the map's centre.
2. Take a point every 5 m along each contour, and interpolate a 5 m
   elevation grid linearly on their Delaunay triangulation.
3. Fill depressions with a priority flood (Barnes et al., 2014). Each
   connected flooded area is a basin, and basins touching the grid edge
   are dropped.
4. A basin at least 1 m deep somewhere is a pond. Several deep spots in one
   basin are one pond, because they fill together.
5. Flow routing: each cell drains to its steepest downhill neighbour (D8).
   Flat ground and lake surfaces drain to their nearest exit, so no water
   gets stuck.
6. The catchment is found by pointer jumping over the flow paths, and the
   outlet is the pond cell through which the most water leaves.

The terrain for a file is cached per worker process, keyed by the SHA-256 of
the file, so `/findCatchment` after `/analyzeContour` on the same file
takes about 15 ms instead of a second. On the provided sample map (8.5 km²),
`/analyzeContour` takes about 1 s.

## Limits and settings

| Setting | Value | Where |
| --- | --- | --- |
| Upload size | 100 MB | `MAX_UPLOAD_BYTES` in `app/routes/contour.py` |
| KML size after unpacking a KMZ | 200 MB | `MAX_KML_BYTES` |
| Grid size | 6,000,000 cells (about 150 km² at 5 m) | `MAX_DEM_CELLS` |
| Grid cell size / contour sampling | 5 m / 5 m | `RESOLUTION_M`, `SAMPLE_SPACING_M` |
| Minimum pond depth | 1 m | `MIN_POND_DEPTH_M` |
| Cache | 6 maps per worker, each up to 2,000,000 cells | `CACHE_ENTRIES`, `CACHE_MAX_CELLS` |

The last five are in `app/services/contour_analyzer.py`.

## Using it from Python

```python
from app.services.contour_analyzer import analyze_ponds, find_catchment

ponds = analyze_ponds("contours_1m.kml")          # a path or the file's bytes
catchment = find_catchment("contours_1m.kml", ponds[0]["pond_id"])
```

`parse_kml(path)` still returns contours in the version 1.0 format, for old
scripts.

## Notes

- **CORS:** the API has no CORS middleware. The frontend's development
  server proxies `/api` to it. To call the API directly from a web page on
  another origin, add FastAPI's `CORSMiddleware` in `app/main.py`.
- **Caching:** with several workers, a `/findCatchment` request can reach a
  worker that hasn't seen the file yet. It then rebuilds the terrain (about
  1 s) and gets the same result.
- **Accuracy:** catchments depend on the contour interval. With 1 m
  contours, flat ground that could drain either way makes them uncertain by
  about 10–20%. Depths and storage are more robust.

## Project layout

```
app/
  main.py                    FastAPI app; starts the background warm-up
  routes/contour.py          endpoints, upload handling, error responses
  services/contour_analyzer.py
                             parsing, elevation grid, priority flood, ponds,
                             flow routing, catchment, cache
requirements.txt             pinned dependencies
```

## Changes in version 1.1

The endpoints, field names and response shapes are unchanged; `spill` gained
`latitude` and `longitude`.

- **Catchment:** it was the flow count at one cell on the pond's edge
  (0.09 ha for the sample map's largest pond); it is now the whole
  catchment (388 ha).
- **Outlet:** it was reported 1 m below the real overflow level.
- **Ponds:** hollows in one basin are one pond, numbered by storage, and the
  volume includes the shallow edge.
- **Input:** files without the standard namespace, KMZ, other elevation
  fields, MultiGeometry and one-point lines now work, and the analysis uses
  the map's own UTM zone.
- **Concurrency:** there is no shared `dem.tif` and no files named after
  the client's upload, so simultaneous requests and several workers are
  safe. The endpoints no longer block the server.
- **Speed:** vectorised sampling cut `/analyzeContour` on the sample map
  from about 4 s to about 1 s, and the per-file cache cut `/findCatchment`
  after it from about 4 s to about 15 ms.
- **Libraries:** pysheds, rasterio, shapely and matplotlib are no longer
  used. pysheds left some lake cells without a flow direction, which
  stopped water from reaching downstream ponds.
