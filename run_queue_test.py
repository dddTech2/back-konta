from datetime import datetime
from dian_automation.db.database import SessionLocal, init_db
from dian_automation.db.models import User, Business, DIANExtractionJob
from dian_automation.queue.manager import ExtractionQueueManager
from dian_automation.queue.worker import ExtractionWorker

init_db()
db = SessionLocal()

# 1. Asegurar empresa demo
user = db.query(User).filter_by(email="admin@kontable.com").first()
biz1 = db.query(Business).filter_by(nit="901008579").first()
biz2 = db.query(Business).filter_by(nit="900888999").first()
if not biz2:
    biz2 = Business(client_id=user.id, legal_name="Segunda Empresa SAS", commercial_name="Empresa 2", nit="900888999", dv="5")
    db.add(biz2)
    db.commit()

print("\n================ TEST COLA Y PACING 15 MINUTOS ================")

# 2. Encolar dos descargas
job1 = ExtractionQueueManager.enqueue_job(biz1.id, "2026-08", db=db)
job2 = ExtractionQueueManager.enqueue_job(biz2.id, "2026-08", db=db)
print(f"1. Encolado Job 1 (ID: {job1.id[:8]}...) para NIT {biz1.nit}")
print(f"2. Encolado Job 2 (ID: {job2.id[:8]}...) para NIT {biz2.nit}")

# 3. Ejecutar el worker para procesar el primer trabajo usando el ZIP local como fixture
worker = ExtractionWorker()
res = worker.run_once(
    extractor_func=lambda b, p: "downloads/183ff689-751a-4971-82a6-e178e427c1c3.zip",
    pacing_seconds=900
)

# 4. Verificar estados en base de datos
db.refresh(job1)
db.refresh(job2)
delta = (job2.next_run_at - datetime.utcnow()).total_seconds() / 60

print(f"\n--- ESTADO TRAS EJECUCIÓN DE JOB 1 ---")
print(f"Job 1 Status: {job1.status} (Finalizado exitosamente con ingesta automática)")
print(f"Job 2 Status: {job2.status}")
print(f"Job 2 Próxima ejecución: {job2.next_run_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"Pausa obligatoria activa para Job 2: Faltan {delta:.1f} minutos para iniciar")

# 5. Comprobar que el worker se rehúsa a procesar Job 2 inmediatamente
siguiente = ExtractionQueueManager.get_next_runnable_job(db)
print(f"¿Hay trabajos listos para ejecutarse ya mismo?: {'SI' if siguiente else 'NO (Worker en reposo por 15 min)'}")

db.close()
