"""Pruebas de la Story 1.8 (AC #3 a #6): subida del ZIP con reintento, pending_uploads/, bucle a prueba
de excepciones y archivos de despliegue. El API se simula con httpx.MockTransport."""

from pathlib import Path

import httpx
import pytest

import run_worker_remote as worker
from dian_automation.queue.exceptions import is_slow_error

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ZIP_BYTES = b"PK\x03\x04contenido-del-zip"


class FakeApi:
    """Responde en orden con lo programado (Response, o una excepción que se lanza); si se agota, repite `default`."""

    def __init__(self, *responses, default=None):
        self.responses = list(responses)
        self.default = default if default is not None else httpx.Response(200, json={"status": "SUCCESS"})
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, Exception):
            item.request = request
            raise item
        return item

    def paths(self):
        return [r.url.path for r in self.requests]

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


class Clock:
    """Reloj y sleep falsos: dormir solo adelanta la hora, así los 20 min de reintentos no tardan nada."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


@pytest.fixture(autouse=True)
def isolated_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "API_BASE", "http://api.test")
    monkeypatch.setattr(worker, "WORKER_TOKEN", "secreto")
    monkeypatch.setattr(worker, "WORKER_NAME", "casa")
    monkeypatch.setattr(worker, "PENDING_UPLOADS_DIR", tmp_path / "pending_uploads")
    monkeypatch.setattr(worker, "EVIDENCE_CANDIDATES", [])


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def zip_file(tmp_path):
    path = tmp_path / "descarga.zip"
    path.write_bytes(ZIP_BYTES)
    return path


def pending_dir():
    return worker.PENDING_UPLOADS_DIR


def pending_names():
    return sorted(p.name for p in pending_dir().iterdir()) if pending_dir().is_dir() else []


def upload(client, clock, job_id="job-1", path="x.zip"):
    return worker.upload_with_retry(client, job_id, str(path), sleep=clock.sleep, monotonic=clock.monotonic)


# ---------------------------------------------------------------------------
# Latido: cabecera del worker
# ---------------------------------------------------------------------------

def test_fetch_next_job_sends_the_token_and_the_worker_name():
    api = FakeApi(httpx.Response(200, json={"job": {"job_id": "j1"}}))

    job = worker.fetch_next_job(api.client())

    assert job == {"job_id": "j1"}
    (request,) = api.requests
    assert request.url == "http://api.test/internal/jobs/next"
    assert request.headers["Authorization"] == "Bearer secreto"
    assert request.headers["X-Worker-Name"] == "casa"


# ---------------------------------------------------------------------------
# AC #3: reintento de la subida
# ---------------------------------------------------------------------------

def test_a_transient_failure_is_retried_with_growing_waits_until_it_works(zip_file, clock):
    api = FakeApi(
        httpx.ConnectError("sin red"),
        httpx.Response(503, text="no disponible"),
        httpx.Response(200, json={"status": "SUCCESS", "job_id": "job-1"}),
    )

    result = upload(api.client(), clock, path=zip_file)

    assert result["status"] == "SUCCESS"
    assert clock.sleeps == [5, 10]
    assert api.paths() == ["/internal/jobs/job-1/complete"] * 3
    assert zip_file.read_bytes() == ZIP_BYTES


def test_a_timeout_counts_as_a_network_failure(zip_file, clock):
    api = FakeApi(httpx.ReadTimeout("lento"), httpx.Response(200, json={"status": "SUCCESS"}))

    assert upload(api.client(), clock, path=zip_file) == {"status": "SUCCESS"}
    assert clock.sleeps == [5]


def test_the_wait_doubles_up_to_5_minutes_and_gives_up_after_20(zip_file, clock):
    api = FakeApi(default=httpx.Response(502))

    with pytest.raises(worker.UploadNotDelivered):
        upload(api.client(), clock, path=zip_file)

    assert clock.sleeps[:7] == [5, 10, 20, 40, 80, 160, 300]
    assert max(clock.sleeps) == 300
    assert sum(clock.sleeps) == 20 * 60
    assert len(api.requests) == len(clock.sleeps) + 1


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_a_4xx_is_never_retried(zip_file, clock, status):
    api = FakeApi(httpx.Response(status, json={"detail": "ZIP inválido"}))

    with pytest.raises(worker.UploadRejected) as rejected:
        upload(api.client(), clock, path=zip_file)

    assert rejected.value.status_code == status and "ZIP inválido" in rejected.value.detail
    assert len(api.requests) == 1 and clock.sleeps == []


def test_report_success_uploads_and_keeps_nothing_pending(zip_file, clock):
    api = FakeApi()

    worker.report_success(api.client(), "job-1", str(zip_file))

    assert api.paths() == ["/internal/jobs/job-1/complete"]
    assert pending_names() == []


def test_report_success_parks_the_zip_when_the_upload_is_exhausted(monkeypatch, zip_file):
    def exhausted(client, job_id, path):
        raise worker.UploadNotDelivered("sin red")

    monkeypatch.setattr(worker, "upload_with_retry", exhausted)

    worker.report_success(FakeApi().client(), "job-9", str(zip_file))

    assert pending_names() == ["job-9.zip"]
    assert (pending_dir() / "job-9.zip").read_bytes() == ZIP_BYTES
    assert not zip_file.exists()  # se movió, no se copió


def test_the_zip_survives_a_real_network_outage_end_to_end(monkeypatch, zip_file, clock):
    api = FakeApi(default=httpx.ConnectError("sin red"))
    real = worker.upload_with_retry
    monkeypatch.setattr(worker, "upload_with_retry", lambda client, job_id, path: real(client, job_id, path, clock.sleep, clock.monotonic))

    worker.report_success(api.client(), "job-3", str(zip_file))  # no lanza

    assert pending_names() == ["job-3.zip"]
    assert (pending_dir() / "job-3.zip").read_bytes() == ZIP_BYTES
    assert set(api.paths()) == {"/internal/jobs/job-3/complete"}


def test_report_success_reports_a_hard_failure_on_a_422_without_retrying(zip_file):
    api = FakeApi(httpx.Response(422, json={"detail": "no se pudo parsear"}), httpx.Response(200, json={"status": "RETRY_SCHEDULED"}))

    worker.report_success(api.client(), "job-4", str(zip_file))

    assert api.paths() == ["/internal/jobs/job-4/complete", "/internal/jobs/job-4/fail"]
    body = api.requests[1].read().decode("utf-8", "ignore")
    assert "UploadRejected" in body and "422" in body
    assert not is_slow_error("UploadRejected")  # es un fallo duro: TECH_OPS y pausa de 1 h
    assert pending_names() == []


# ---------------------------------------------------------------------------
# AC #3: pending_uploads/ al iniciar y en cada vuelta
# ---------------------------------------------------------------------------

def put_pending(job_id="job-1", content=ZIP_BYTES):
    pending_dir().mkdir(parents=True, exist_ok=True)
    path = pending_dir() / f"{job_id}.zip"
    path.write_bytes(content)
    return path


def test_pending_uploads_are_sent_once_each_and_deleted(clock):
    put_pending("job-a")
    put_pending("job-b")
    api = FakeApi()

    worker.retry_pending_uploads(api.client())

    assert api.paths() == ["/internal/jobs/job-a/complete", "/internal/jobs/job-b/complete"]
    assert pending_names() == []


def test_a_pending_zip_of_a_job_already_success_is_discarded():
    put_pending("job-1")
    api = FakeApi(httpx.Response(200, json={"status": "SUCCESS", "job_id": "job-1", "ignored": True}))

    worker.retry_pending_uploads(api.client())

    assert pending_names() == []


def test_a_pending_zip_the_api_cannot_take_stays_and_the_pass_stops():
    put_pending("job-a")
    put_pending("job-b")
    api = FakeApi(default=httpx.ConnectError("sin red"))

    worker.retry_pending_uploads(api.client())

    assert api.paths() == ["/internal/jobs/job-a/complete"]  # no insiste con el resto
    assert pending_names() == ["job-a.zip", "job-b.zip"]


def test_a_5xx_on_a_pending_zip_keeps_it_for_the_next_round():
    put_pending("job-a")

    worker.retry_pending_uploads(FakeApi(httpx.Response(503)).client())

    assert pending_names() == ["job-a.zip"]


def test_a_rejected_pending_zip_is_set_aside_and_not_retried_again():
    put_pending("job-a")
    api = FakeApi(httpx.Response(422, json={"detail": "inválido"}))

    worker.retry_pending_uploads(api.client())
    worker.retry_pending_uploads(api.client())

    assert pending_names() == ["job-a.zip.rejected"]
    assert len(api.requests) == 1


def test_retrying_pending_uploads_without_the_folder_does_nothing():
    api = FakeApi()

    worker.retry_pending_uploads(api.client())

    assert api.requests == []


def test_non_zip_files_in_the_folder_are_left_alone():
    put_pending("job-a")
    (pending_dir() / "notas.txt").write_text("x")

    worker.retry_pending_uploads(FakeApi().client())

    assert pending_names() == ["notas.txt"]


# ---------------------------------------------------------------------------
# AC #4: el bucle principal nunca termina
# ---------------------------------------------------------------------------

def run_loop(client, iterations, sleeps=None, signals=None):
    sleeps = [] if sleeps is None else sleeps
    worker.worker_loop(
        client,
        max_iterations=iterations,
        sleep=sleeps.append,
        wait_signal=lambda timeout_seconds: signals.append(timeout_seconds) if signals is not None else None,
    )
    return sleeps


def test_the_loop_retries_pending_uploads_before_asking_for_a_job():
    put_pending("job-a")
    api = FakeApi(default=httpx.Response(200, json={"job": None}))

    run_loop(api.client(), 1)

    assert api.paths() == ["/internal/jobs/job-a/complete", "/internal/jobs/next"]
    assert pending_names() == []


def test_an_unexpected_exception_is_logged_and_the_loop_continues(monkeypatch, caplog):
    calls = []

    def flaky_fetch(client):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("fallo inesperado")
        return None

    monkeypatch.setattr(worker, "fetch_next_job", flaky_fetch)

    with caplog.at_level("ERROR"):
        sleeps = run_loop(FakeApi().client(), 4)

    assert len(calls) == 4  # nunca terminó
    assert sleeps[:2] == [worker.POLL_INTERVAL_SECONDS] * 2  # espera y sigue tras cada error
    assert "Error inesperado en el bucle del worker" in caplog.text


def test_an_api_outage_is_logged_and_the_loop_keeps_polling(caplog):
    api = FakeApi(default=httpx.ConnectError("sin red"))

    with caplog.at_level("WARNING"):
        run_loop(api.client(), 3)

    assert api.paths() == ["/internal/jobs/next"] * 3
    assert "No se pudo consultar la API" in caplog.text


def test_a_failing_job_does_not_end_the_loop(monkeypatch):
    jobs = [{"job_id": "j1", "business_id": "b", "target_period": "2026-09", "login_type": "persona"}, None]
    monkeypatch.setattr(worker, "fetch_next_job", lambda client: jobs.pop(0))

    async def boom(**kwargs):
        raise RuntimeError("la DIAN cambió")

    monkeypatch.setattr(worker, "run_flow", boom)
    api = FakeApi(httpx.Response(200, json={"status": "RETRY_SCHEDULED"}))

    run_loop(api.client(), 2)

    assert api.paths() == ["/internal/jobs/j1/fail"]
    assert b"RuntimeError" in api.requests[0].read()


def test_a_job_whose_zip_cannot_be_uploaded_is_parked_and_the_loop_goes_on(monkeypatch, zip_file, clock):
    jobs = [{"job_id": "j2", "business_id": "b", "target_period": "2026-09", "login_type": "persona"}, None]
    monkeypatch.setattr(worker, "fetch_next_job", lambda client: jobs.pop(0))

    async def fake_flow(**kwargs):
        return str(zip_file)

    monkeypatch.setattr(worker, "run_flow", fake_flow)
    real = worker.upload_with_retry
    monkeypatch.setattr(worker, "upload_with_retry", lambda client, job_id, path: real(client, job_id, path, clock.sleep, clock.monotonic))
    api = FakeApi(default=httpx.ConnectError("sin red"))

    run_loop(api.client(), 2)

    assert pending_names() == ["j2.zip"]


def test_ctrl_c_still_stops_the_loop(monkeypatch):
    def interrupt(client):
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "fetch_next_job", interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_loop(FakeApi().client(), 5)


def test_main_refuses_to_start_without_a_token(monkeypatch, caplog):
    monkeypatch.setattr(worker, "WORKER_TOKEN", "")
    monkeypatch.setattr(worker, "worker_loop", lambda client: pytest.fail("no debe arrancar"))

    with caplog.at_level("ERROR"):
        worker.main()

    assert "INTERNAL_WORKER_TOKEN" in caplog.text


# ---------------------------------------------------------------------------
# AC #5 y #6: arranque automático y documentación
# ---------------------------------------------------------------------------

def test_the_windows_task_installer_registers_and_removes_kontable_worker():
    script = (PROJECT_ROOT / "scripts" / "install_worker_task.ps1").read_text(encoding="utf-8-sig")

    assert '"Kontable Worker"' in script
    assert "-AtLogOn" in script  # arranca al iniciar sesión
    assert "-RestartCount" in script and "-RestartInterval" in script  # se reinicia ante fallos
    assert "$Uninstall" in script and "Unregister-ScheduledTask" in script  # opción inversa
    assert "run_worker_remote.py" in script


def test_the_deployment_guide_covers_requirements_install_heartbeat_and_the_silence_alert():
    guide = (PROJECT_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    for required in (
        "Chrome", "KONTABLE_API_URL", "INTERNAL_WORKER_TOKEN", "REDIS_URL", "STALWART_",  # requisitos
        "install_worker_task.ps1", "-Uninstall", "[Service]", "systemctl",  # Windows y systemd
        "worker_heartbeats", "last_seen_at",  # verificar el latido
        "no responde", "WORKER_SILENCE_MINUTES", "pending_uploads",  # qué hacer con el aviso
    ):
        assert required in guide, required
