from elasticsearch import AsyncElasticsearch

from app.core.config import settings

_client: AsyncElasticsearch | None = None

GPS_INDEX = settings.gps_index

# geo_point powers geo_distance ("buses within X km"), geo_bounding_box
# (map-viewport queries) and geohash-grid aggregations (heatmaps).
GPS_MAPPING = {
    "mappings": {
        "properties": {
            "bus_id": {"type": "keyword"},
            "helper_id": {"type": "keyword"},
            "trip_id": {"type": "keyword"},
            "ts": {"type": "date"},
            "location": {"type": "geo_point"},  # {lat, lon}
            "speed": {"type": "float"},
            "heading": {"type": "float"},
            "accuracy": {"type": "float"},
        }
    }
}


def get_es_client() -> AsyncElasticsearch:
    """Process-wide async Elasticsearch client."""
    global _client
    if _client is None:
        kwargs: dict = {"hosts": [settings.elasticsearch_url]}
        if settings.elasticsearch_user and settings.elasticsearch_password:
            kwargs["basic_auth"] = (settings.elasticsearch_user, settings.elasticsearch_password)
        _client = AsyncElasticsearch(**kwargs)
    return _client


async def get_es() -> AsyncElasticsearch:
    """FastAPI dependency."""
    return get_es_client()


async def ensure_gps_index(es: AsyncElasticsearch) -> None:
    """Create the gps_points index with the geo_point mapping if absent."""
    if not await es.indices.exists(index=GPS_INDEX):
        await es.indices.create(index=GPS_INDEX, body=GPS_MAPPING)
