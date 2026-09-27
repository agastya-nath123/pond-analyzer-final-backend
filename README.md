# Pond Analysis Backend

Backend service for automated **pond-site detection and catchment-area analysis** from topographic contour data.

The system accepts a KML file containing contour lines, converts the contour information into a Digital Elevation Model (DEM), performs terrain and hydrological analysis, identifies potential pond/depression locations, and calculates the contributing catchment area for a selected pond.

The backend is implemented using **FastAPI** and exposes REST API endpoints for contour analysis and catchment analysis.

---

## Table of Contents

- [Overview](#overview)
- [Objectives](#objectives)
- [How the System Works](#how-the-system-works)
- [System Workflow](#system-workflow)
- [Project Structure](#project-structure)
- [Technology Stack](#technology-stack)
- [Installation](#installation)
- [Running the Backend](#running-the-backend)
- [API Documentation](#api-documentation)
- [API Endpoints](#api-endpoints)
  - [`GET /`](#get-)
  - [`POST /analyzeContour`](#post-analyzecontour)
  - [`POST /findCatchment`](#post-findcatchment)
- [Input KML Format](#input-kml-format)
- [Pond Detection Methodology](#pond-detection-methodology)
- [Catchment Analysis Methodology](#catchment-analysis-methodology)
- [Pond IDs](#pond-ids)
- [Important Parameters](#important-parameters)
- [Coordinate Reference System](#coordinate-reference-system)
- [Error Handling](#error-handling)
- [Current Limitations](#current-limitations)
- [Future Improvements](#future-improvements)
- [Example Workflow](#example-workflow)
- [Development Notes](#development-notes)
- [Quick Start](#quick-start)
- [Summary](#summary)

---

## Overview

The Pond Analysis Backend is a geospatial and hydrological processing service designed to identify potential pond locations from topographic contour data.

Instead of requiring a pre-existing DEM, the backend constructs a DEM directly from contour lines supplied in a KML file.

The generated terrain model is then processed to determine:

- Terrain depressions
- Depression area
- Maximum depression depth
- Estimated water-storage volume
- Candidate spill points
- Flow direction
- Flow accumulation
- Contributing catchment area
- Catchment-to-pond area ratio

The backend separates the analysis into two major operations:

1. **Pond detection** using `/analyzeContour`
2. **Catchment calculation** for a selected pond using `/findCatchment`

---

## Objectives

The main objectives of the backend are:

- Accept topographic contour data in KML format.
- Convert geographic coordinates into a metric projected coordinate system.
- Generate a raster Digital Elevation Model from contour lines.
- Identify topographic depressions that may represent pond sites.
- Estimate the physical characteristics of each detected depression.
- Determine a candidate spill point for a selected depression.
- Calculate the upstream contributing catchment area.
- Provide the results through simple REST APIs that can be consumed by a frontend application.

---

## How the System Works

The backend follows a terrain-analysis pipeline:

```text
                    KML Contour File
                           |
                           v
                    Parse KML File
                           |
                           v
              Extract Contour Lines
                    + Elevations
                           |
                           v
                Project Coordinates
                 EPSG:4326 → EPSG:32644
                           |
                           v
              Sample Points Along Lines
                           |
                           v
                  Generate DEM
                           |
                           v
               Fill Depressions
                           |
                           v
                  Resolve Flats
                           |
                           v
                Calculate Flow Direction
                           |
                           v
                Calculate Flow Accumulation
                           |
                           v
                 Detect Depressions
                           |
                           v
             Calculate Pond Characteristics
                           |
                           +------------------+
                           |                  |
                           v                  v
                   Pond Candidates      Selected Pond ID
                                              |
                                              v
                                      Find Spill Point
                                              |
                                              v
                                      Flow Accumulation
                                              |
                                              v
                                      Catchment Area
```

---

## System Workflow

### 1. KML Upload

The client uploads a KML file containing topographic contour lines. The KML is parsed using Python's XML processing facilities. Each valid contour is expected to contain:

- an elevation stored in the Placemark's `<name>` element,
- a `LineString`, and
- coordinate information.

### 2. Coordinate Projection

KML coordinates are normally represented as longitude and latitude in WGS84. The backend converts these coordinates from:

- **EPSG:4326** → **EPSG:32644** (UTM Zone 44N)

This conversion is important because the subsequent calculations require distances and areas in metres.

### 3. Contour Sampling

The contour lines are converted into Shapely `LineString` objects. Points are sampled along the contours at approximately **5 metres** apart. Each sampled point retains the elevation of its source contour.

The resulting data therefore consists of `(x, y, elevation)` samples.

### 4. DEM Generation

The sampled contour points are interpolated onto a regular raster grid. Linear interpolation is used to generate the Digital Elevation Model.

The current DEM resolution is **5 m × 5 m**. Therefore, one raster cell represents **25 m²** of terrain.

---

## Project Structure

```text
pond-backend/
│
├── app/
│   ├── main.py
│   │
│   ├── routes/
│   │   └── contour.py
│   │
│   └── services/
│       └── contour_analyzer.py
│
├── uploads/
│   └── # Uploaded KML files
│
├── requirements.txt
│
├── .gitignore
│
└── .venv/
    └── # Python virtual environment
```

**`app/main.py`**
Initializes the FastAPI application and registers the API routers.

**`app/routes/contour.py`**
Contains the HTTP API endpoints. The main endpoints are:

```text
GET  /
POST /analyzeContour
POST /findCatchment
```

The route layer is responsible for:

- receiving uploaded files,
- receiving request parameters,
- storing uploaded KML files,
- calling the analysis service, and
- returning JSON responses.

**`app/services/contour_analyzer.py`**
Contains the main geospatial and hydrological processing logic. Responsibilities include:

- KML parsing
- coordinate projection
- contour sampling
- DEM generation
- depression filling
- flat resolution
- flow-direction calculation
- flow-accumulation calculation
- depression detection
- pond analysis
- catchment analysis

Keeping this processing separate from the API routes allows the computational logic to be reused independently of FastAPI.

---

## Technology Stack

### Backend Framework

**FastAPI** — provides the HTTP API layer and handles:

- REST endpoints
- file uploads
- form parameters
- request validation
- JSON responses

### Programming Language

Python 3.11

### Geospatial Libraries

| Library | Purpose |
|---|---|
| **PyProj** | Coordinate transformation (`EPSG:4326` → `EPSG:32644`) |
| **Shapely** | Geometric operations and contour-line processing |
| **Rasterio** | Raster DEM representation and GeoTIFF creation |

### Numerical and Scientific Libraries

| Library | Purpose |
|---|---|
| **NumPy** | Raster arrays, mathematical operations, masks, distance calculations, numerical processing |
| **SciPy** | Interpolation (`scipy.interpolate.griddata`), connected-component labeling (`scipy.ndimage.label`), other numerical operations |

### Hydrological Processing

**Pysheds** — used for raster-based hydrological analysis including:

- depression filling
- flat resolution
- flow direction
- flow accumulation

### Other Libraries

| Library | Purpose |
|---|---|
| **ElementTree** | Parses the XML structure of KML files |
| **Heap Queue** | Used by the custom priority-flood depression-filling implementation |
| **Matplotlib** | Available for visualization and analysis during development |

---

### Project Layout

```code
.
├── app
│   ├── __init__.py
│   ├── main.py
│   ├── routes
│   │   ├── __init__.py
│   │   └── contour.py
│   └── services
│       ├── __init__.py
│       └── contour_analyzer.py
├── README.md
├── requirements.txt
└── uploads
```

## Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd pond-backend
```

### 2. Create a Virtual Environment

```bash
python3.11 -m venv .venv
```

Activate it:

**macOS / Linux**

```bash
source .venv/bin/activate
```

**Windows**

```bash
.venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Running the Backend

From the project root:

```bash
uvicorn app.main:app --reload
```

The backend will normally start at:

```
http://127.0.0.1:8000
```

---

## API Documentation

FastAPI automatically generates interactive API documentation.

- **Swagger UI:** `http://127.0.0.1:8000/docs`
- **ReDoc:** `http://127.0.0.1:8000/redoc`

These interfaces can be used to upload KML files and test the endpoints without requiring a separate frontend.

---

## API Endpoints

### `GET /`

Checks whether the backend is running.

**Request**
No parameters are required.

**Response**

```json
{
  "message": "Pond Analysis API is running"
}
```

---

### `POST /analyzeContour`

Analyzes an uploaded KML contour dataset and returns detected potential pond locations.

**Purpose**
This endpoint performs the complete terrain-analysis pipeline. It takes the contour lines from the uploaded KML file and generates a DEM from which potential pond depressions are detected.

**Request**

- Content type: `multipart/form-data`
- Parameter: `contour_map` — the KML contour dataset (e.g. `file = contours.kml`)

**Processing**

1. KML parsing
2. Coordinate projection
3. Contour sampling
4. DEM generation
5. Depression filling
6. Flat resolution
7. Flow-direction calculation
8. Flow-accumulation calculation
9. Depression detection
10. Pond characterization

**Response**

```json
{
  "ponds": [
    {
      "pond_id": 67,
      "pond_area_ha": 2.1925,
      "max_depth_m": 2.9325,
      "volume_m3": 46492.68
    }
  ]
}
```

**Response fields**

| Field | Description |
|---|---|
| `pond_id` | Identifier of the detected connected depression, generated by connected-component labeling. Used to select the pond in `/findCatchment`. |
| `pond_area_ha` | Estimated horizontal area of the depression in hectares. Calculated as `(number of depression cells × cell area) / 10,000`. With a 5 m resolution, `cell area = 5 × 5 = 25 m²`. |
| `max_depth_m` | Maximum estimated depth of the depression, calculated from `filled DEM - original DEM`. Represents the maximum amount by which the original terrain must be raised to fill the depression to its spill level. |
| `volume_m3` | Estimated water-storage volume of the depression, approximated as `Σ(depression depth of each cell × cell area)`. |

---

### `POST /findCatchment`

Calculates the contributing catchment area for a selected pond.

**Purpose**
This endpoint is intended to be used after `/analyzeContour`. The client selects a `pond_id` returned by `/analyzeContour` and submits the original KML file plus the selected `pond_id`. The backend then reconstructs the terrain model and determines how much upstream terrain drains toward the selected pond's spill point.

**Request**

- Content type: `multipart/form-data`
- Parameters:
  - `contour_map` (e.g. `contours.kml`)
  - `pond_id` (e.g. `67`)

**Processing**

1. Parses the KML.
2. Projects the contour coordinates.
3. Samples the contours.
4. Generates the DEM.
5. Fills depressions.
6. Resolves flat regions.
7. Calculates D8 flow direction.
8. Calculates flow accumulation.
9. Reconstructs the depression labels.
10. Selects the requested depression using `pond_id`.
11. Determines its candidate spill point.
12. Finds the raster cell corresponding to the spill point.
13. Reads the flow accumulation at that cell.
14. Converts accumulated cells into catchment area.
15. Calculates the catchment-to-pond area ratio.

**Response**

```json
{
  "pond_id": 67,
  "spill": {
    "easting": 382145.0,
    "northing": 2345678.0,
    "elevation_m": 142.35
  },
  "flow_accumulation_cells": 53788,
  "catchment_area_m2": 1344700.0,
  "catchment_area_ha": 134.47,
  "catchment_pond_ratio": 61.33
}
```

**Response fields**

| Field | Description |
|---|---|
| `pond_id` | The identifier of the selected depression. |
| `spill.easting` | Projected X coordinate of the spill point. |
| `spill.northing` | Projected Y coordinate of the spill point. |
| `spill.elevation_m` | Elevation of the selected raster cell at the spill location. |
| `flow_accumulation_cells` | Number of upstream raster cells contributing flow to the spill cell. |
| `catchment_area_m2` | Estimated contributing catchment area in square metres, calculated as `flow accumulation cells × cell area`. |
| `catchment_area_ha` | Catchment area converted to hectares (`catchment_area_m2 / 10,000`). |
| `catchment_pond_ratio` | Ratio of contributing catchment area to pond area (`catchment_area_ha / pond_area_ha`). |

**Example**

```
Catchment = 134.47 ha
Pond      = 2.1925 ha
Ratio     ≈ 61.33
```

This means the contributing catchment is approximately 61 times the area of the detected pond.

---

## Input KML Format

The backend expects KML contour data containing `Placemark` elements. A simplified structure is:

```xml
<Placemark>
    <name>120</name>
    <LineString>
        <coordinates>
            77.123,28.456,0
            77.124,28.457,0
            77.125,28.458,0
        </coordinates>
    </LineString>
</Placemark>
```

The backend interprets `<name>120</name>` as **Elevation = 120 metres**, and extracts the longitude and latitude from `longitude,latitude,altitude`.

> **Note:** The altitude component is currently not used to determine elevation because the elevation is taken from the Placemark name.

---

## Pond Detection Methodology

### Depression Depth

After filling the DEM:

```text
fill_depth = filled_dem - dem
```

This gives the estimated depth of each cell within a topographic depression. The current implementation identifies depression cells using:

```text
depression_mask = fill_depth >= 1.0
```

Therefore, cells requiring less than 1 metre of filling are not included in the depression mask.

### Connected Components

The depression mask is divided into connected regions using `scipy.ndimage.label()`. Each connected region receives an integer identifier (Region 1, Region 2, Region 3, ... Region 67, ...). These identifiers become the `pond_id` values returned by `/analyzeContour`.

### Boundary Filtering

Depressions touching the outer boundary of the DEM are ignored. This prevents terrain extending outside the available contour coverage from being incorrectly interpreted as an enclosed pond.

### Area Calculation

For a raster resolution of 5 m, cell area = 25 m². If a depression contains 500 cells:

```text
Area = 500 × 25
     = 12,500 m²
     = 1.25 ha
```

### Volume Calculation

For each depression cell:

```text
depth = filled_elevation - original_elevation
```

The volume is approximated by summing the depth contribution of all cells:

```text
volume = Σ(depth × cell_area)
```

---

## Catchment Analysis Methodology

The catchment analysis is based on raster flow accumulation.

### D8 Flow Direction

Each DEM cell is evaluated against its neighboring cells. The cell's flow direction is assigned toward the neighboring cell with the greatest downward slope. Diagonal neighbors use the diagonal distance (`resolution × √2`), while horizontal and vertical neighbors use `resolution`.

### Flow Accumulation

Once the flow direction raster is generated, flow accumulation determines how many upstream cells contribute to each cell. A high accumulation value generally indicates a drainage or stream-like location.

```text
Cell accumulation = 5,000
Cell size = 25 m²

Catchment area = 5,000 × 25
               = 125,000 m²
               = 12.5 ha
```

### Spill Point Calculation

For each depression, the backend identifies cells along the depression boundary. The filled DEM is used to estimate the elevation at which water would spill from the depression. The boundary cell whose original elevation is closest to the calculated spill elevation is selected as the candidate spill point.

The spill point is returned in projected coordinates. It is therefore an **estimated raster-derived location**, not a field-surveyed measurement.

---

## Pond IDs

The `pond_id` returned by `/analyzeContour` is generated dynamically using connected-component labeling.

```json
{ "pond_id": 67 }
```

This does **not** mean that pond 67 is a globally registered pond. It means: *Connected depression region number 67 in this particular DEM analysis.*

Therefore, `pond_id` should be treated as a **temporary analysis identifier**. The same KML data and processing configuration must be used when requesting `/findCatchment`.

---

## Important Parameters

| Parameter | Current Value | Notes |
|---|---|---|
| **DEM Resolution** | `5 m × 5 m` | Smaller resolutions can potentially provide greater spatial detail but may increase computational cost and interpolation sensitivity. |
| **Contour Sampling Spacing** | `~5 metres` | Controls how densely the contour lines contribute points to DEM interpolation. |
| **Depression Depth Threshold** | `fill_depth >= 1.0` | Only cells with a calculated fill depth of at least 1 metre are classified as depression cells. This is a methodological parameter and should be adjusted according to the project's requirements and terrain characteristics. |

---

## Coordinate Reference System

The input KML coordinates are interpreted as **EPSG:4326 (WGS84)**. They are transformed to **EPSG:32644 (UTM Zone 44N)**.

The projected system allows calculations such as distance, area, raster cell size, and catchment area to be performed using metres.

> The configured UTM zone must correspond to the geographic location of the input dataset. If data from another region is supplied, the CRS configuration may need to be changed.

---

## Error Handling

The API distinguishes between invalid pond selections and unexpected processing failures.

- If a requested `pond_id` does not exist, `Pond <pond_id> not found` is returned as an error.
- Unexpected failures during KML parsing, DEM generation, raster processing, or hydrological analysis are returned as server errors.

---

## Current Limitations

1. **Contour Quality** — DEM quality depends heavily on contour accuracy, contour spacing, contour completeness, elevation correctness, and interpolation quality. Poor contour data produces a poor DEM.
2. **Fixed CRS** — The current implementation assumes EPSG:32644 for projected calculations. This should eventually be determined automatically or configured based on the study area.
3. **Spill Point Is Estimated** — The calculated spill point is derived from the raster and should not be considered a precise engineering or survey measurement. Field validation may be required before constructing an actual pond or water-retention structure.
4. **Rivers Are Not Explicitly Removed** — The current pond-detection algorithm identifies terrain depressions. It does not yet have an explicit river/stream dataset or river exclusion layer. Consequently, a river channel or drainage feature could potentially be identified as a depression under some terrain conditions. A future implementation should incorporate a river/stream layer or derive a stream network from flow accumulation and exclude existing watercourses from pond candidates.
5. **Pond Detection Is Terrain-Based** — The system detects topographic depressions. It does not independently determine whether a detected depression is an actual existing pond. A detected candidate therefore means *"potential topographic pond/depression"* rather than *"confirmed physical pond."*
6. **No Intermediate Result Cache** — `/findCatchment` currently reconstructs the DEM and hydrological model from the KML file instead of reusing the calculations performed by `/analyzeContour`. This means the same KML may be processed multiple times. A future version could cache the DEM, filled DEM, flow direction, flow accumulation, and depression labels, and associate those results with an analysis ID.
7. **Temporary DEM File** — The current implementation writes the generated DEM to `dem.tif`. This approach is suitable for development and single-user testing but can cause conflicts if multiple requests are processed simultaneously. A production implementation should use unique temporary files, in-memory raster processing, or a persistent raster-data management system.

---

## Future Improvements

### River Exclusion

Integrate river/stream data or derive streams from flow accumulation.

```text
DEM
 ↓
Flow Accumulation
 ↓
Stream Network
 ↓
Exclude Existing Rivers
 ↓
Potential Pond Sites
```

### Dynamic CRS Selection

Automatically determine the appropriate UTM zone from the input KML instead of assuming EPSG:32644.

### Intermediate Result Caching

Store the generated terrain and hydrological products so that `/findCatchment` does not need to repeat the entire analysis.

```text
KML
 ↓
/analyzeContour
 ↓
analysis_id
 ↓
Stored DEM + Hydrology
 ↓
/findCatchment
 ↓
pond_id
 ↓
Catchment
```

### Improved Pond Ranking

Candidate ponds could eventually be ranked according to:

- pond area
- maximum depth
- estimated storage volume
- catchment area
- catchment-to-pond ratio
- proximity to streams
- terrain slope
- accessibility
- other project-specific constraints

### Frontend Integration

The backend can be consumed by a web frontend that:

1. uploads a KML file,
2. displays detected pond candidates,
3. allows the user to select a pond,
4. requests its catchment,
5. displays the catchment and spill information on a map.

---

## Example Workflow

A typical client workflow is:

```text
1. Upload contour KML
          |
          v
2. POST /analyzeContour
          |
          v
3. Receive list of pond candidates
          |
          v
4. User selects pond_id
          |
          v
5. POST /findCatchment
       + pond_id
       + KML
          |
          v
6. Receive catchment information
          |
          v
7. Display results on frontend
```

**Step 1** — `POST /analyzeContour` with `contours.kml`

```json
{
  "ponds": [
    {
      "pond_id": 67,
      "pond_area_ha": 2.1925,
      "max_depth_m": 2.9325,
      "volume_m3": 46492.68
    }
  ]
}
```

**Step 2** — Select `pond_id = 67`

**Step 3** — `POST /findCatchment` with `contours.kml` and `pond_id = 67`

**Step 4** — Receive:

```json
{
  "pond_id": 67,
  "spill": {
    "easting": 382145.0,
    "northing": 2345678.0,
    "elevation_m": 142.35
  },
  "flow_accumulation_cells": 53788,
  "catchment_area_m2": 1344700.0,
  "catchment_area_ha": 134.47,
  "catchment_pond_ratio": 61.33
}
```

---

## Development Notes

The backend is currently intended primarily for development and experimentation. The computational pipeline is deterministic for the same:

- KML input,
- contour sampling spacing,
- DEM resolution,
- coordinate reference system, and
- hydrological processing parameters.

This consistency is important because the `pond_id` generated by connected-component labeling depends on the resulting raster.

For production deployment, additional work should be performed around:

- request validation
- file-size limits
- secure uploaded-file handling
- concurrent processing
- temporary-file management
- caching
- CRS detection
- computational resource limits
- structured logging
- API authentication
- persistent storage of analysis results

---

## Quick Start

```bash
# Clone
git clone <repository-url>

# Enter project
cd pond-backend

# Create environment
python3.11 -m venv .venv

# Activate
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start server
uvicorn app.main:app --reload
```

Then open: `http://127.0.0.1:8000/docs`

Upload a KML file through `/analyzeContour`, select a returned `pond_id`, and use `/findCatchment` to calculate its contributing catchment.

---

## Summary

The Pond Analysis Backend converts contour-line data into a terrain and hydrological model and uses that model to identify potential pond sites and calculate their contributing catchments.

The core processing pipeline is:

```text
KML Contours
     ↓
Coordinate Projection
     ↓
Contour Sampling
     ↓
DEM Generation
     ↓
Depression Filling
     ↓
Flat Resolution
     ↓
D8 Flow Direction
     ↓
Flow Accumulation
     ↓
Depression Detection
     ↓
Pond Characterization
     ↓
Spill Point Detection
     ↓
Catchment Analysis
```

The backend exposes three primary operations:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | API health/status |
| `POST` | `/analyzeContour` | Detect potential ponds |
| `POST` | `/findCatchment` | Calculate catchment for a selected pond |

The resulting system provides a foundation for a larger pond-site analysis platform in which terrain, hydrology, existing watercourses, and additional geographical constraints can be combined to rank and visualize suitable pond locations.
