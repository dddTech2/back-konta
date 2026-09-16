"""Pruebas unitarias para el parser de archivos Excel/ZIP de la DIAN."""

import io
import os
import zipfile
from datetime import datetime
from decimal import Decimal
import pytest
import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dian_automation.db.database import Base
from dian_automation.db.models import User, Business, Invoice, MonthlyTaxSummary
from dian_automation.extraction.xlsx_parser import DIANXLSXParser, DIANParseError


@pytest.fixture
def db_session():
    """Crea una base de datos SQLite en memoria aislada para cada test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    try:
        # Sembrar usuario y negocio de prueba
        user = User(
            id="user-test-1",
            email="andrea@test.com",
            full_name="Andrea Torres",
            phone="3001234567",
            role="CLIENT",
        )
        db.add(user)
        business = Business(
            id="biz-test-1",
            client_id=user.id,
            legal_name="Andrea Torres Diseño SAS",
            commercial_name="Andrea Torres Diseño",
            nit="901008579",
            dv="8",
        )
        db.add(business)
        db.commit()
        yield db
    finally:
        db.close()


def test_parse_real_dian_zip_fixture(db_session):
    """Verifica el procesamiento del archivo ZIP real descargado de la DIAN."""
    zip_path = os.path.join(
        os.path.dirname(__file__), "..", "downloads", "183ff689-751a-4971-82a6-e178e427c1c3.zip"
    )
    if not os.path.exists(zip_path):
        pytest.skip("Fixture real de ZIP no encontrado en downloads/")

    result = DIANXLSXParser.parse_zip(
        zip_path=zip_path,
        business_id="biz-test-1",
        db=db_session,
    )

    assert result["invoices_processed"] > 0
    assert len(result["periods"]) > 0

    # Verificar que las facturas se guardaron en la BD
    invoices = db_session.query(Invoice).filter(Invoice.business_id == "biz-test-1").all()
    assert len(invoices) == result["invoices_processed"]

    # Verificar CUFE único y tipos
    sample_inv = invoices[0]
    assert sample_inv.cufe is not None
    assert isinstance(sample_inv.iva, Decimal)
    assert isinstance(sample_inv.total, Decimal)
    assert sample_inv.group_type in ("Emitido", "Recibido")

    # Verificar que se generó al menos un resumen mensual
    summary = db_session.query(MonthlyTaxSummary).filter(MonthlyTaxSummary.business_id == "biz-test-1").first()
    assert summary is not None
    assert isinstance(summary.iva_generado, Decimal)
    assert isinstance(summary.iva_descontable, Decimal)
    assert summary.iva_balance == (summary.iva_generado - summary.iva_descontable)


def test_credit_note_algebra_and_grouping(db_session):
    """Verifica que las notas crédito resten algebraicamente del IVA y del Total neto."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rp_Doc_Test"

    # Encabezados estándar DIAN
    headers = [
        "Tipo de documento", "CUFE/CUDE", "Folio", "Prefijo", "Divisa", "Forma de Pago", "Medio de Pago",
        "Fecha Emisión", "Fecha Recepción", "NIT Emisor", "Nombre Emisor", "NIT Receptor", "Nombre Receptor",
        "IVA", "ICA", "IC", "INC", "Timbre", "INC Bolsas", "IN Carbono", "IN Combustibles", "IC Datos",
        "ICL", "INPP", "IBUA", "ICUI", "Rete IVA", "Rete Renta", "Rete ICA", "Total", "Estado", "Grupo"
    ]
    ws.append(headers)

    # 1. Factura Emitida (Venta): Total 100.000, IVA 19.000
    ws.append([
        "Factura electrónica", "CUFE-001", "101", "FE", "COP", "1", "48",
        "15-08-2026 10:00:00", "15-08-2026 10:05:00", "901008579", "Andrea Torres", "2222", "Cliente A",
        19000, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 100000, "Aprobado", "Emitido"
    ])

    # 2. Nota Crédito Emitida: Total 10.000, IVA 1.900 (debe RESTAR)
    ws.append([
        "Nota de crédito electrónica", "CUFE-002", "102", "NC", "COP", "1", "48",
        "16-08-2026 11:00:00", "16-08-2026 11:05:00", "901008579", "Andrea Torres", "2222", "Cliente A",
        1900, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 10000, "Aprobado", "Emitido"
    ])

    # 3. Factura Recibida (Compra): Total 50.000, IVA 9.500 (Descontable)
    ws.append([
        "Factura electrónica", "CUFE-003", "501", "FE", "COP", "1", "48",
        "17-08-2026 12:00:00", "17-08-2026 12:05:00", "800123456", "Proveedor B", "901008579", "Andrea Torres",
        9500, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 50000, "Aprobado", "Recibido"
    ])

    # Guardar en memoria como ZIP
    excel_buf = io.BytesIO()
    wb.save(excel_buf)
    excel_buf.seek(0)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as z:
        z.writestr("test_report.xlsx", excel_buf.getvalue())
    zip_buf.seek(0)

    result = DIANXLSXParser.parse_zip(
        zip_path=zip_buf,
        business_id="biz-test-1",
        db=db_session,
    )

    summary = result["summaries"]["2026-08"]

    # Validar álgebra exacta:
    # IVA Generado Neto = 19.000 - 1.900 = 17.100
    assert summary["iva_generado"] == Decimal("17100.00")
    # IVA Descontable Neto = 9.500
    assert summary["iva_descontable"] == Decimal("9500.00")
    # Saldo a Pagar = 17.100 - 9.500 = 7.600
    assert summary["iva_balance"] == Decimal("7600.00")
    # Total facturado neto = 100.000 - 10.000 = 90.000
    assert summary["total_invoiced_net"] == Decimal("90000.00")
    assert summary["total_invoices_count"] == 3


def test_upsert_idempotence(db_session):
    """Verifica que procesar el mismo archivo dos veces actualice sin duplicar facturas."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Tipo de documento", "CUFE/CUDE", "Fecha Emisión", "IVA", "Total", "Grupo"])
    ws.append(["Factura electrónica", "CUFE-IDEMPOTENT", "20-08-2026", 1900, 10000, "Emitido"])

    excel_buf = io.BytesIO()
    wb.save(excel_buf)
    excel_buf.seek(0)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w") as z:
        z.writestr("idempotent.xlsx", excel_buf.getvalue())
    zip_buf.seek(0)

    # Primera pasada
    DIANXLSXParser.parse_zip(zip_path=zip_buf, business_id="biz-test-1", db=db_session)
    count_1 = db_session.query(Invoice).filter(Invoice.cufe == "CUFE-IDEMPOTENT").count()
    assert count_1 == 1

    # Segunda pasada
    zip_buf.seek(0)
    DIANXLSXParser.parse_zip(zip_path=zip_buf, business_id="biz-test-1", db=db_session)
    count_2 = db_session.query(Invoice).filter(Invoice.cufe == "CUFE-IDEMPOTENT").count()
    assert count_2 == 1


def test_corrupt_zip_handling(db_session):
    """Verifica que un archivo ZIP inválido o corrupto arroje DIANParseError."""
    corrupt_buf = io.BytesIO(b"no-es-un-zip-valido-123456")
    with pytest.raises(DIANParseError, match="Archivo ZIP corrupto o no válido"):
        DIANXLSXParser.parse_zip(zip_path=corrupt_buf, business_id="biz-test-1", db=db_session)
