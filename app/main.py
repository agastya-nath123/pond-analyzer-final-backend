import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes.contour import router as contour_router
from app.services.contour_analyzer import warm_up


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Compile the analysis code in the background, so the first real
    # request doesn't wait for it and the server starts answering at once.
    threading.Thread(target=warm_up, daemon=True).start()
    yield


app = FastAPI(
    title="Pond Planning API",
    description="""
    Backend API for automated pond and catchment analysis.

    The backend accepts KML or KMZ contour data, generates a Digital Elevation Model
    (DEM), performs hydrological analysis, identifies potential pond locations,
    and calculates the catchment area and related characteristics for selected
    ponds.
    """,
    version="1.1.0",
    lifespan=lifespan,
)

app.include_router(contour_router)

@app.get("/")
def root():
    """
    Return basic information about the Pond Analysis API.

    This endpoint serves as the root entry point of the backend API.
    It can be used to verify that the FastAPI application is running
    and accessible.

    Unlike the analysis endpoints, this endpoint does not perform any
    terrain, DEM, hydrological, or pond-related calculations.

    Returns:
        200:
            A JSON object containing a simple status message indicating
            that the API is operational.

    Example response:

        {
            "message": "Pond Analysis API is running"
        }
    """

    return {
        "message": "Pond Planning API is running"
    }
