"""Script de demostración y verificación interactiva para Story 1.4:
Canal y Bot de Telegram para Alertas Técnicas a Soporte TI.
"""

import os
import sys
import json
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dian_automation.db.models import Base, User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.telegram.tech_ops_bot import TechOpsAlertBot


def main():
    print("=" * 70)
    print("[VERIFICACION] STORY 1.4: BOT DE TELEGRAM PARA ALERTAS A TECH-OPS")
    print("=" * 70)

    # 1. Base de datos aislada para demostración
    engine = create_engine("sqlite:///verify_story_1_4.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # 2. Sembrar usuarios tri-rol
    tech_ops_user = User(
        id="usr-tech-demo",
        email="alex.devops@kontable.co",
        full_name="Alex DevOps (Soporte TI)",
        role="TECH_OPS",
        telegram_chat_id=987654321,
        is_active=True,
    )
    admin_user = User(
        id="usr-admin-demo",
        email="katerinn.admin@kontable.co",
        full_name="Katerinn (Admin Comercial)",
        role="ADMIN",
        telegram_chat_id=555444333,
        is_active=True,
    )
    client_user = User(
        id="usr-client-demo",
        email="andrea.client@kontable.co",
        full_name="Andrea Torres (Contribuyente)",
        role="CLIENT",
        telegram_chat_id=111222333,
        is_active=True,
    )

    biz = Business(
        id="biz-demo-14",
        client_id=client_user.id,
        legal_name="Andrea Torres Diseño SAS",
        commercial_name="Torres Branding",
        nit="901888999",
        dv="4",
    )

    db.add_all([tech_ops_user, admin_user, client_user, biz])
    db.commit()

    print("\n[OK] 1. Usuarios registrados en el sistema:")
    print(f"   - TECH_OPS: {tech_ops_user.full_name} | Chat ID: {tech_ops_user.telegram_chat_id}")
    print(f"   - ADMIN:    {admin_user.full_name} | Chat ID: {admin_user.telegram_chat_id}")
    print(f"   - CLIENT:   {client_user.full_name} | Chat ID: {client_user.telegram_chat_id}")

    # 3. Encolar y simular fallo de extracción
    job = ExtractionQueueManager.enqueue_job(biz.id, "2026-08", db)
    os.makedirs("downloads", exist_ok=True)
    screenshot_evidence = f"downloads/evidence_{job.id}.png"
    with open(screenshot_evidence, "w", encoding="utf-8") as f:
        f.write("SIMULATED_SCREENSHOT_CLOUDFLARE_TURNSTILE_BLOCK")

    job = ExtractionQueueManager.mark_job_failed(
        job_id=job.id,
        error_code="TURNSTILE_BLOCKED",
        error_detail="Cloudflare Turnstile token no superado tras 45 segundos",
        screenshot_path=screenshot_evidence,
        db=db,
        backoff_seconds=3600,
    )

    print("\n[FAIL] 2. Tarea de extracción fallida en orquestador:")
    print(f"   - Job ID: {job.id}")
    print(f"   - Error: {job.error_code} ({job.error_detail})")
    print(f"   - Próxima ejecución programada: {job.next_run_at.strftime('%H:%M:%S')} (Pausa de 1 hora)")
    print(f"   - Evidencia capturada: {job.screenshot_path}")

    # 4. Capturar el despacho hacia Telegram mediante mock_dispatcher
    telegram_dispatches = []

    def mock_telegram_dispatcher(endpoint, data, files):
        telegram_dispatches.append({"endpoint": endpoint, "data": data, "files": files})
        return {"ok": True, "result": {"message_id": 9001}}

    bot = TechOpsAlertBot(bot_token="test_bot_token", http_dispatcher=mock_telegram_dispatcher)

    print("\n[DISPATCH] 3. Despachando alerta a canales de Telegram...")
    alert_summary = bot.send_failure_alert(job=job, db=db)

    print(f"   - Total destinatarios alertados: {alert_summary['recipients_count']}")
    print(f"   - Tipo de mensaje Telegram: {alert_summary['endpoint']}")
    print(f"   - ¿Adjunta captura?: {alert_summary['has_screenshot']}")

    # Inspeccionar el mensaje recibido por Tech-Ops
    call = telegram_dispatches[0]
    print(f"\n📩 4. Mensaje entregado a Telegram Chat ID {call['data']['chat_id']}:")
    print("------------------------------------------------------------")
    print(call["data"].get("caption") or call["data"].get("text"))
    print("------------------------------------------------------------")
    if call["files"]:
        print(f"📸 Foto adjunta: {call['files']['photo']}")

    reply_markup = json.loads(call["data"]["reply_markup"])
    button = reply_markup["inline_keyboard"][0][0]
    print(f"🔘 Botón interactivo: [{button['text']}] -> Callback: '{button['callback_data']}'")

    # 5. Simular clic del operador en el botón '🔄 Reintentar Ahora'
    print(f"\n⚡ 5. Operador presiona el botón interactivo '{button['text']}'...")
    callback_res = TechOpsAlertBot.handle_retry_callback(button["callback_data"], db)
    print(f"   - Respuesta de Telegram Bot: {callback_res['message']}")
    print(f"   - Nuevo estado del job: {callback_res['new_status']}")

    db.refresh(job)
    print(f"   - Próxima ejecución actualizada en DB: {job.next_run_at.strftime('%H:%M:%S')} (¡INMEDIATA!)")

    # 6. Comprobar que el orquestador ahora sí lo toma para ejecución
    next_job = ExtractionQueueManager.get_next_runnable_job(db)
    print("\n🔍 6. Consulta de la cola de extracciones:")
    print(f"   -> Trabajo tomado para ejecutar: Job ID={next_job.id[:8]} (Empresa: {biz.commercial_name})")

    # Limpieza
    if os.path.exists(screenshot_evidence):
        os.remove(screenshot_evidence)
    db.close()
    engine.dispose()
    if os.path.exists("verify_story_1_4.db"):
        try:
            os.remove("verify_story_1_4.db")
        except Exception:
            pass

    print("\n" + "=" * 70)
    print("[OK] VERIFICACION DE STORY 1.4 COMPLETADA EXITOSAMENTE")
    print("=" * 70)


if __name__ == "__main__":
    main()
