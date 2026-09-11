FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mmu_server.py .
COPY neo4j_layer.py .
COPY light_index_v2.py .
COPY ingest.py .

VOLUME ["/data"]
EXPOSE 8765

# Defaults for a bare `docker run`. docker-compose.yml overrides most of
# these, which is exactly what let the dead ones below rot unnoticed.
#
# Removed here, because nothing reads them:
#   MMU_DB_PATH        pointed at a SQLite file. SQLite has not been the data
#                      store since Phase 3; neo4j_layer.py opens with
#                      "SQLite removed. Neo4j is the sole data store."
#   NEO4J_READS        read by no code in the repository.
#   MMU_USE_V2_INDEX   likewise. The v2 index is not optional any more -- it
#                      IS the read path.
#   MMU_V2_INDEX_PATH  the server reads MMU_INDEX_PATH (light_index_v2.py).
#                      Only the host-side migration script uses this name.
ENV MMU_INDEX_PATH=/data/memory_index_v2.json
ENV MMU_ARCHIVE_THRESH=20
ENV NEO4J_URI=bolt://neo4j:7687
ENV NEO4J_USER=neo4j
ENV NEO4J_ENABLED=true
ENV MMU_DOC_ROOT=/docs

CMD ["uvicorn", "mmu_server:app", "--host", "0.0.0.0", "--port", "8765"]
