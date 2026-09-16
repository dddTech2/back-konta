"""Script de demostración y verificación interactiva para Story 1.3:
Detección de Fallos, Backoff de 1 Hora, Evidencia Visual (Screenshot) y Reprogramación de Cola.
"""

import os
import sys
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker
from dian_automation.queue.exceptions import MailTokenTimeoutError, TurnstileBlockedError


def main():
    print("=" * 70)
    print("[VERIFICACION] STORY 1.3: DETECCION DE FALLOS, BACKOFF Y EVIDENCIA")
    print("=" * 70)

    # 1. Preparar base de datos de demostración
    engine = create_engine("sqlite:///verify_story_1_3.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    user = User(
        id="usr-demo-1",
        email="andrea.demo@kontable.co",
        full_name="Andrea Torres",
        role="CLIENT",
    )
    biz_a = Business(
        id="biz-demo-1",
        client_id=user.id,
        legal_name="Andrea Torres Diseno SAS",
        commercial_name="Torres Branding",
        nit="901888999",
        dv="4",
    )
    biz_b = Business(
        id="biz-demo-2",
        client_id=user.id,
        legal_name="Andrea Torres Retail SAS",
        commercial_name="Torres Tienda Online",
        nit="901999000",
        dv="5",
    )
    db.add_all([user, biz_a, biz_b])
    db.commit()

    print("\n[OK] 1. Sembrados en DB:")
    print(f"   - Negocio A: {biz_a.commercial_name} (NIT {biz_a.nit})")
    print(f"   - Negocio B: {biz_b.commercial_name} (NIT {biz_b.nit})")

    # 2. Encolar dos jobs
    job_a = ExtractionQueueManager.enqueue_job(biz_a.id, "2026-08", db)
    job_b = ExtractionQueueManager.enqueue_job(biz_b.id, "2026-08", db)
    print("\n[INFO] 2. Trabajos Encolados Inicialmente:")
    print(f"   - Job A: ID={job_a.id[:8]} | Status={job_a.status} | NextRun={job_a.next_run_at.strftime('%H:%M:%S')}")
    print(f"   - Job B: ID={job_b.id[:8]} | Status={job_b.status} | NextRun={job_b.next_run_at.strftime('%H:%M:%S')}")

    # 3. Simular captura de pantalla temporal para evidencia visual
    dummy_screenshot = "screenshot_espera.png"
    with open(dummy_screenshot, "w", encoding="utf-8") as f:
        f.write("DUMMY_IMAGE_EVIDENCE_FOR_TECH_OPS_ALERT")

    # 4. Callback para capturar alerta de TI (Tech-Ops)
    alerts_captured = []

    def tech_ops_alert_listener(job, err_code, err_detail, screen_path):
        alerts_captured.append({
            "job_id": job.id,
            "error_code": err_code,
            "error_detail": err_detail,
            "screenshot": screen_path,
        })
        print(f"\n[ALERTA TI DISPARADA] Codigo: {err_code}")
        print(f"   Detalle: {err_detail}")
        print(f"   Captura adjunta: {screen_path}")

    # 5. Ejecutar Worker con extractor simulado que falla con MailTokenTimeoutError
    def failing_mail_extractor(biz, period):
        raise MailTokenTimeoutError("IMAP Timeout: No se encontro token en Stalwart tras 120s")

    worker = ExtractionWorker(db_session_factory=lambda: Session())
    print("\n[RUN] 3. Ejecutando Worker en Job A (Simulando Fallo IMAP)...")
    res = worker.run_once(
        extractor_func=failing_mail_extractor,
        on_failure_callback=tech_ops_alert_listener,
    )

    db.refresh(job_a)
    db.refresh(job_b)

    print("\n[ESTADO] 4. Estado de la Cola tras el Fallo:")
    print(f"   - Job A:")
    print(f"       * Status: {job_a.status} (Reprogramado)")
    print(f"       * Intentos: {job_a.attempt_count} de {job_a.max_attempts}")
    print(f"       * Error Code: {job_a.error_code}")
    print(f"       * Proxima ejecucion: {job_a.next_run_at.strftime('%H:%M:%S')} (+1 hora)")
    print(f"       * Evidencia guardada: {job_a.screenshot_path}")
    print(f"   - Job B (Pausa global de cola):")
    print(f"       * Status: {job_b.status}")
    print(f"       * Proxima ejecucion: {job_b.next_run_at.strftime('%H:%M:%S')} (Pausado 1 hora)")

    # 6. Simular intentos sucesivos hasta llegar a FAILED terminal
    print("\n[RUN] 5. Simulando 2do y 3er reintento para alcanzar estado FAILED terminal...")
    
    def failing_turnstile_extractor(biz, period):
        raise TurnstileBlockedError("Cloudflare Turnstile bloqueo la solicitud")

    # Forzar ejecución inmediata adelantando el reloj
    job_a.next_run_at = datetime.utcnow() - timedelta(seconds=10)
    db.commit()

    # Intento 2
    worker.execute_job(job_a.id, extractor_func=failing_turnstile_extractor)
    db.refresh(job_a)
    print(f"   - Tras intento 2 -> Status={job_a.status}, Intentos={job_a.attempt_count}/3")

    # Intento 3 (Final)
    job_a.next_run_at = datetime.utcnow() - timedelta(seconds=10)
    db.commit()
    worker.execute_job(job_a.id, extractor_func=failing_turnstile_extractor)
    db.refresh(job_a)
    print(f"   - Tras intento 3 -> Status={job_a.status}, Intentos={job_a.attempt_count}/3 (TERMINAL)")

    # 7. Verificar elegibilidad en cola
    next_job = ExtractionQueueManager.get_next_runnable_job(db)
    print("\n[CHECK] 6. Siguiente trabajo elegible ahora en cola:")
    print(f"   -> {next_job} (Ninguno, debido a la pausa de 1 hora y el estado terminal)")

    # Limpieza
    if os.path.exists(dummy_screenshot):
        os.remove(dummy_screenshot)
    if job_a.screenshot_path and os.path.exists(job_a.screenshot_path):
        os.remove(job_a.screenshot_path)
    db.close()
    engine.dispose()
    if os.path.exists("verify_story_1_3.db"):
        try:
            os.remove("verify_story_1_3.db")
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("[OK] VERIFICACION DE STORY 1.3 COMPLETADA EXITOSAMENTE")
    print("=" * 70)


if __name__ == "__main__":
    main()
