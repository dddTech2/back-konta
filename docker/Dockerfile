# Imagen unica reutilizada por los 3 servicios (api / bot / worker) via docker-compose,
# seleccionando el proceso con el comando pasado al entrypoint. Incluye Chrome real + Xvfb
# porque el worker de extraccion DIAN depende de un Chrome nativo con pantalla (ver ADR-001:
# evasion de Cloudflare Turnstile) -- no es Playwright quien lanza el navegador, solo se
# conecta via CDP a este Chrome. api y bot no usan Chrome, pero comparten la misma imagen
# por simplicidad en esta primera prueba de despliegue.
FROM python:3.14-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates xvfb \
        fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 \
        libasound2 libxss1 libgbm1 libxshmfence1 \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
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
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
