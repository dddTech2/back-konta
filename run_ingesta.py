from dian_automation.db.database import SessionLocal, init_db
from dian_automation.db.models import User, Business, Invoice, MonthlyTaxSummary
from dian_automation.extraction.xlsx_parser import DIANXLSXParser

init_db()
db = SessionLocal()

user = db.query(User).filter_by(email='admin@kontable.com').first()
if not user:
    user = User(email='admin@kontable.com', full_name='Katerinn', role='ADMIN')
    db.add(user)
    db.commit()

biz = db.query(Business).filter_by(nit='901008579').first()
if not biz:
    biz = Business(client_id=user.id, legal_name='Veterinaria Demo SAS', commercial_name='Vet Demo', nit='901008579', dv='8')
    db.add(biz)
    db.commit()

res = DIANXLSXParser.parse_zip('downloads/183ff689-751a-4971-82a6-e178e427c1c3.zip', business_id=biz.id, db=db)
print('\n================ REPORTE DE INGESTA DIAN ================')
print(f"Total Facturas Procesadas: {res['invoices_processed']}")
print(f"Periodos Fiscales Detectados: {res['periods']}")

for periodo, summary in res['summaries'].items():
    print(f'\n--- RESUMEN MENSUAL PERIODO: {periodo} ---')
    print(f"Total Facturado Neto:     ${summary['total_invoiced_net']:,.2f}")
    print(f"IVA Generado (Ventas):    ${summary['iva_generado']:,.2f}")
    print(f"IVA Descontable (Gastos): ${summary['iva_descontable']:,.2f}")
    print(f"Saldo a Pagar DIAN:       ${summary['iva_balance']:,.2f}")
    print(f"Rete IVA Total:           ${summary['rete_iva_total']:,.2f}")
    print(f"Rete Renta Total:         ${summary['rete_renta_total']:,.2f}")
    print(f"Rete ICA Total:           ${summary['rete_ica_total']:,.2f}")

db.close()
