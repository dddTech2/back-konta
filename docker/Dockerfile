# Imagen unica reutilizada por los servicios (api / bot / scheduler / migrate) via docker-compose,
# seleccionando el proceso con el comando pasado al entrypoint. No incluye Chrome ni Xvfb: la extraccion
# de la DIAN la hace el worker remoto, fuera del VPS (docs/DEPLOYMENT.md).
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Capa de dependencias separada del codigo para que `docker build` no reinstale todo
# cada vez que cambia un .py.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
