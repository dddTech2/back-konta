"""Señal ligera de 'hay trabajo nuevo' vía Redis, para despertar al instante al worker
remoto (corriendo en una red residencial, fuera de la IP bloqueada del VPS) en vez de
depender solo de su propio polling.

La base de datos sigue siendo la única fuente de verdad del estado de los jobs -- Redis
aquí es solo un timbre, nunca almacena el trabajo en sí. Si Redis no está disponible o
no está configurado, el encolado normal sigue funcionando igual (el consumidor remoto
cae de vuelta a polling por su cuenta).
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("redis_signal")

QUEUE_KEY = "dian:jobs:ready"


def _get_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis
        return redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    except Exception as e:
        logger.warning(f"No se pudo construir el cliente de Redis: {e}")
        return None


def notify_job_ready(job_id: str) -> None:
    """Avisa por Redis que hay un trabajo listo. Nunca lanza excepción: si Redis falla,
    el flujo de encolado normal (basado en la base de datos) sigue intacto."""
    client = _get_client()
    if not client:
        return
    try:
        client.lpush(QUEUE_KEY, job_id)
    except Exception as e:
        logger.warning(f"No se pudo notificar a Redis (se seguirá por polling): {e}")


def wait_for_job_signal(timeout_seconds: int = 25) -> Optional[str]:
    """Bloquea hasta timeout_seconds esperando una señal. Usado por el consumidor remoto
    para reaccionar al instante en vez de esperar su propio intervalo de polling."""
    client = _get_client()
    if not client:
        return None
    try:
        result = client.blpop(QUEUE_KEY, timeout=timeout_seconds)
        if result:
            _, job_id = result
            return job_id.decode() if isinstance(job_id, bytes) else job_id
    except Exception as e:
        logger.warning(f"Redis no disponible para esperar señal: {e}")
    return None
