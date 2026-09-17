"""Pruebas para la señal ligera de Redis: debe degradarse a no-op si Redis no está
configurado o no está disponible, sin romper nunca el flujo de encolado normal."""

from dian_automation.queue import redis_signal


def test_notify_job_ready_is_noop_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_signal.notify_job_ready("job-123")  # no debe lanzar excepción


def test_wait_for_job_signal_returns_none_without_redis_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert redis_signal.wait_for_job_signal(timeout_seconds=1) is None


def test_notify_job_ready_swallows_connection_errors(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://usuario-invalido:0/0")

    class FakeClient:
        def lpush(self, *args, **kwargs):
            raise ConnectionError("Redis no disponible en la prueba")

    monkeypatch.setattr(redis_signal, "_get_client", lambda: FakeClient())
    redis_signal.notify_job_ready("job-456")  # no debe lanzar excepción


def test_wait_for_job_signal_decodes_bytes(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")

    class FakeClient:
        def blpop(self, key, timeout):
            return (key, b"job-789")

    monkeypatch.setattr(redis_signal, "_get_client", lambda: FakeClient())
    assert redis_signal.wait_for_job_signal(timeout_seconds=1) == "job-789"
