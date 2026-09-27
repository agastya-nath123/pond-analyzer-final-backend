import logging

from fastapi import (
    APIRouter,
    UploadFile,
    File,
    Form,
    HTTPException
)
from pydantic import BaseModel

from app.services.contour_analyzer import (
    ContourMapError,
    MapTooLargeError,
    PondNotFoundError,
    analyze_ponds,
    find_catchment
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Uploads larger than this are refused before any processing.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class Pond(BaseModel):
    pond_id: int
    pond_area_ha: float
    max_depth_m: float
    volume_m3: float


class AnalyzeContourResponse(BaseModel):
    ponds: list[Pond]


class SpillPoint(BaseModel):
    easting: float
    northing: float
    elevation_m: float
    # Added in 1.1; older clients ignore them.
    latitude: float | None = None
    longitude: float | None = None


class CatchmentResponse(BaseModel):
    pond_id: int
    spill: SpillPoint
    flow_accumulation_cells: int
    catchment_area_m2: float
    catchment_area_ha: float
    catchment_pond_ratio: float


def _read_upload(contour_map: UploadFile) -> bytes:
    """
    Read the upload into memory. Nothing is written to disk, so requests
    can't overwrite each other's files and the client's file name is never
    used as a path.
    """
    data = contour_map.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return data


def _error_response(error: Exception) -> HTTPException:
    if isinstance(error, MapTooLargeError):
        return HTTPException(status_code=413, detail=str(error))
    if isinstance(error, ContourMapError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(error, PondNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    logger.exception("Contour analysis failed")
    return HTTPException(
        status_code=500,
        detail=f"The analysis failed unexpectedly ({type(error).__name__}: {error})."
    )

# -------------------------
# /analyzeContour
# -------------------------

# The endpoints are plain (not async) functions, so FastAPI runs each
# request in its worker thread pool. The analysis is CPU-heavy; inside an
# async function it would block the server from answering anything else.

@router.post("/analyzeContour",
             response_model=AnalyzeContourResponse
)
def analyze_contour(
    contour_map: UploadFile = File(...)
):
    """
    Analyze a KML or KMZ file of topographic contour lines and identify
    potential pond locations: natural depressions where water collects.

    Each contour must be a LineString placemark (MultiGeometry is fine).
    Its elevation is read from the placemark <name> ("270" or "Contour
    270 m"), from an ExtendedData field such as ELEV or CONTOUR, or from
    the altitude of its coordinates. Any KML namespace is accepted.

    Processing performed by this endpoint:

    1. The file is read (KMZ archives are unpacked) and every contour line
       with an elevation is extracted.

    2. The coordinates are projected from WGS84 (EPSG:4326) into the UTM
       zone the map lies in, so distances and areas are in metres.

    3. Points are sampled along the contour lines every 5 metres.

    4. A Digital Elevation Model (DEM) with 5 m cells is interpolated from
       the samples with linear interpolation.

    5. Depressions are filled with a priority flood, producing the
       surface water would have if every depression were full.

    6. Each connected area the flood raised is a basin. Basins touching the
       edge of the grid are discarded, since the terrain beyond them is
       unknown.

    7. A basin at least 1 metre deep somewhere is a pond candidate. Several
       deep spots in one basin are one pond, since they fill together.

    Response:

        A list of pond candidates, largest storage first:

        - pond_id:
            1 for the pond that stores the most water, then 2, 3, ... Use
            it with /findCatchment and the same file.

        - pond_area_ha:
            Area of the pond that is 1 metre deep or more, in hectares.

        - max_depth_m:
            Greatest water depth when the pond is full, in metres.

        - volume_m3:
            Water the whole basin holds when full, in cubic metres,
            including its shallow edge.

    Request:
        Content-Type: multipart/form-data

        contour_map:
            A KML or KMZ file containing contour lines and their elevations.

    Returns:
        200:
            JSON object containing the detected pond candidates.

        400:
            The file isn't a usable contour map. The message says why.

        413:
            The file, or the ground it covers, is larger than the server
            analyses at once.

        500:
            An unexpected error occurred during the analysis.

    Example response:

        {
            "ponds": [
                {
                    "pond_id": 1,
                    "pond_area_ha": 21.74,
                    "max_depth_m": 7.0,
                    "volume_m3": 606865.0
                }
            ]
        }

    Notes:
        pond_id values belong to one analysis of one file. They are not
        permanent geographic identifiers.
    """

    data = _read_upload(contour_map)

    try:
        results = analyze_ponds(
            data,
            resolution=5
        )

        return {
            "ponds": results
        }

    except Exception as e:
        raise _error_response(e)

# -------------------------
# /findCatchment
# -------------------------

@router.post("/findCatchment",
             response_model=CatchmentResponse
)
def find_catchment_endpoint(
    contour_map: UploadFile = File(...),
    pond_id: int = Form(...)
):
    """
    Find where a pond overflows and the catchment that drains into it, for
    a pond_id returned by /analyzeContour for the same file.

    The terrain built by /analyzeContour is cached per file, so this call
    normally takes milliseconds. If it isn't cached (another server
    process, or the cache was full), the terrain is rebuilt from the file
    in exactly the same way, so pond ids always match.

    Processing performed by this endpoint:

    1. The terrain is taken from the cache, or rebuilt as described for
       /analyzeContour.

    2. Flow directions: every cell drains to its steepest downhill
       neighbour (D8) on the filled surface. Flat ground and lake surfaces
       drain towards their nearest exit, so no water is left without a
       path.

    3. The catchment is every cell whose runoff reaches the pond, including
       the catchments of ponds upstream that overflow into it, and the
       pond itself.

    4. The outlet is the edge of the pond through which the most water
       leaves when it overflows.

    Request:

        Content-Type: multipart/form-data

        contour_map:
            The same KML or KMZ file given to /analyzeContour.

        pond_id:
            A pond_id returned by /analyzeContour.

    Returns:
        200:
            JSON object with the pond's outlet and catchment.

        400:
            The file isn't a usable contour map.

        404:
            There is no pond with this pond_id in the file.

        413:
            The file, or the ground it covers, is too large.

        500:
            An unexpected error occurred during the analysis.

    Example response:

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

    Notes:
        spill.easting and spill.northing are in UTM zone 44N (EPSG:32644),
        as in earlier versions, whatever zone the map lies in.
        spill.latitude and spill.longitude give the same point in WGS84.
        spill.elevation_m is the water level at which the pond overflows.

        flow_accumulation_cells is the number of 5 m cells in the
        catchment. Its accuracy depends on the contour interval and the
        interpolation: where flat ground could drain either way, small
        changes in the data can move part of it into a neighbouring
        catchment.
    """

    data = _read_upload(contour_map)

    try:
        result = find_catchment(
            data,
            pond_id,
            resolution=5
        )

        return {
            "pond_id": pond_id,
            **result
        }

    except Exception as e:
        raise _error_response(e)
