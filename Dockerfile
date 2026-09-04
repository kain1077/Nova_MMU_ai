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

ENV MMU_DB_PATH=/data/memory_system.db
ENV MMU_INDEX_PATH=/data/memory_index.json
ENV MMU_ARCHIVE_THRESH=10
ENV NEO4J_URI=bolt://neo4j:7687
ENV NEO4J_USER=neo4j
ENV NEO4J_ENABLED=true
ENV NEO4J_READS=false
ENV MMU_USE_V2_INDEX=false
ENV MMU_V2_INDEX_PATH=/data/memory_index_v2.json
ENV MMU_DOC_ROOT=/docs

CMD ["uvicorn", "mmu_server:app", "--host", "0.0.0.0", "--port", "8765"]
