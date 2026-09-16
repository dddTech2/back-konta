"""Parser de reportes Excel de facturación electrónica DIAN (XLSX/ZIP)."""

import io
import re
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List
import openpyxl
from sqlalchemy.orm import Session

from dian_automation.db.models import Invoice, MonthlyTaxSummary


class DIANParseError(Exception):
    """Excepción para errores al procesar el archivo Excel o ZIP de la DIAN."""
    pass


def _to_decimal(val: Any) -> Decimal:
    """Convierte un valor a Decimal con 2 dígitos de precisión, tratando nulos como 0.00."""
    if val is None or val == "":
        return Decimal("0.00")
    if isinstance(val, (int, float)):
        return Decimal(str(val)).quantize(Decimal("0.01"))
    if isinstance(val, Decimal):
        return val.quantize(Decimal("0.01"))
    try:
        cleaned = str(val).strip().replace("$", "").replace(" ", "").replace(",", "")
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.00")


def _parse_date(val: Any) -> Optional[datetime]:
    """Parsea una celda a datetime, manejando objetos nativos datetime o strings con varios formatos."""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    formats = [
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class DIANXLSXParser:
    """Parser de reportes DIAN VPFE contenidos en libros Excel."""

    @staticmethod
    def extract_xlsx_bytes_from_zip(zip_path_or_bytes: Any) -> bytes:
        """Extrae el contenido en bytes del archivo .xlsx contenido dentro de un ZIP."""
        try:
            if isinstance(zip_path_or_bytes, (str, bytes, io.BytesIO)):
                z = zipfile.ZipFile(zip_path_or_bytes)
            else:
                raise DIANParseError("Tipo de entrada no válido para archivo ZIP")

            xlsx_names = [name for name in z.namelist() if name.lower().endswith(".xlsx")]
            if not xlsx_names:
                raise DIANParseError("No se encontró ningún archivo .xlsx dentro del archivo ZIP")

            return z.read(xlsx_names[0])
        except zipfile.BadZipFile as e:
            raise DIANParseError(f"Archivo ZIP corrupto o no válido: {e}") from e

    @classmethod
    def parse_workbook(
        cls,
        xlsx_bytes: bytes,
        business_id: str,
        db: Session,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Parsea el archivo Excel, persiste las facturas y calcula los resúmenes mensuales."""
        try:
            wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
            sheet = wb.active
            if sheet is None:
                raise DIANParseError("El libro de Excel no contiene hojas válidas")
        except Exception as e:
            raise DIANParseError(f"Error al abrir el libro de Excel con openpyxl: {e}") from e

        rows_iter = sheet.iter_rows(values_only=True)
        try:
            raw_headers = next(rows_iter)
        except StopIteration:
            raise DIANParseError("El archivo Excel está vacío")

        if not raw_headers:
            raise DIANParseError("No se encontraron encabezados en el archivo Excel")

        # Normalizar encabezados (quitar acentos/espacios)
        def clean_h(h):
            if not h:
                return ""
            s = str(h).strip().lower()
            s = re.sub(r"[áäàâ]", "a", s)
            s = re.sub(r"[éëèê]", "e", s)
            s = re.sub(r"[íïìî]", "i", s)
            s = re.sub(r"[óöòô]", "o", s)
            s = re.sub(r"[úüùû]", "u", s)
            return re.sub(r"\s+", " ", s)

        headers = [clean_h(h) for h in raw_headers]
        h_map = {name: idx for idx, name in enumerate(headers) if name}

        def get_col(row, name_pattern, default=None):
            for h_name, idx in h_map.items():
                if name_pattern in h_name:
                    if idx < len(row):
                        return row[idx]
            return default

        invoices_processed = 0
        periods_detected = set()

        for row in rows_iter:
            if not row or all(c is None for c in row):
                continue

            cufe_val = get_col(row, "cufe")
            if not cufe_val:
                continue

            cufe = str(cufe_val).strip()
            doc_type = str(get_col(row, "tipo de documento") or "Factura electrónica").strip()
            folio = str(get_col(row, "folio") or "").strip()
            prefix = str(get_col(row, "prefijo") or "").strip()
            currency = str(get_col(row, "divisa") or "COP").strip()
            payment_form = str(get_col(row, "forma de pago") or "").strip()
            payment_method = str(get_col(row, "medio de pago") or "").strip()

            issue_date = _parse_date(get_col(row, "fecha emision"))
            reception_date = _parse_date(get_col(row, "fecha recepcion"))
            if not issue_date:
                issue_date = reception_date or datetime.utcnow()

            issuer_nit = str(get_col(row, "nit emisor") or "").strip()
            issuer_name = str(get_col(row, "nombre emisor") or "").strip()
            receiver_nit = str(get_col(row, "nit receptor") or "").strip()
            receiver_name = str(get_col(row, "nombre receptor") or "").strip()

            iva = _to_decimal(get_col(row, "iva"))
            ica = _to_decimal(get_col(row, "ica"))
            inc = _to_decimal(get_col(row, "inc"))
            timbre = _to_decimal(get_col(row, "timbre"))
            inc_bolsas = _to_decimal(get_col(row, "inc bolsas"))
            in_carbono = _to_decimal(get_col(row, "in carbono"))
            in_combustibles = _to_decimal(get_col(row, "in combustibles"))
            ibua = _to_decimal(get_col(row, "ibua"))
            icui = _to_decimal(get_col(row, "icui"))
            rete_iva = _to_decimal(get_col(row, "rete iva"))
            rete_renta = _to_decimal(get_col(row, "rete renta"))
            rete_ica = _to_decimal(get_col(row, "rete ica"))
            total = _to_decimal(get_col(row, "total"))

            dian_status = str(get_col(row, "estado") or "").strip()
            group_type = str(get_col(row, "grupo") or "Emitido").strip()

            period_ym = issue_date.strftime("%Y-%m")
            periods_detected.add(period_ym)

            # Upsert en la tabla Invoice
            existing_invoice = db.query(Invoice).filter(Invoice.cufe == cufe).first()
            if existing_invoice:
                existing_invoice.business_id = business_id
                existing_invoice.job_id = job_id
                existing_invoice.document_type = doc_type
                existing_invoice.folio = folio
                existing_invoice.prefix = prefix
                existing_invoice.currency = currency
                existing_invoice.payment_form = payment_form
                existing_invoice.payment_method = payment_method
                existing_invoice.issue_date = issue_date
                existing_invoice.reception_date = reception_date
                existing_invoice.issuer_nit = issuer_nit
                existing_invoice.issuer_name = issuer_name
                existing_invoice.receiver_nit = receiver_nit
                existing_invoice.receiver_name = receiver_name
                existing_invoice.iva = iva
                existing_invoice.ica = ica
                existing_invoice.inc = inc
                existing_invoice.timbre = timbre
                existing_invoice.inc_bolsas = inc_bolsas
                existing_invoice.in_carbono = in_carbono
                existing_invoice.in_combustibles = in_combustibles
                existing_invoice.ibua = ibua
                existing_invoice.icui = icui
                existing_invoice.rete_iva = rete_iva
                existing_invoice.rete_renta = rete_renta
                existing_invoice.rete_ica = rete_ica
                existing_invoice.total = total
                existing_invoice.dian_status = dian_status
                existing_invoice.group_type = group_type
            else:
                inv = Invoice(
                    business_id=business_id,
                    job_id=job_id,
                    document_type=doc_type,
                    cufe=cufe,
                    folio=folio,
                    prefix=prefix,
                    currency=currency,
                    payment_form=payment_form,
                    payment_method=payment_method,
                    issue_date=issue_date,
                    reception_date=reception_date,
                    issuer_nit=issuer_nit,
                    issuer_name=issuer_name,
                    receiver_nit=receiver_nit,
                    receiver_name=receiver_name,
                    iva=iva,
                    ica=ica,
                    inc=inc,
                    timbre=timbre,
                    inc_bolsas=inc_bolsas,
                    in_carbono=in_carbono,
                    in_combustibles=in_combustibles,
                    ibua=ibua,
                    icui=icui,
                    rete_iva=rete_iva,
                    rete_renta=rete_renta,
                    rete_ica=rete_ica,
                    total=total,
                    dian_status=dian_status,
                    group_type=group_type,
                )
                db.add(inv)

            invoices_processed += 1

        db.commit()

        # Recalcular y actualizar MonthlyTaxSummary para cada periodo detectado
        summaries_result = {}
        for ym in periods_detected:
            summary = cls.calculate_monthly_summary(business_id=business_id, period_year_month=ym, db=db)
            summaries_result[ym] = summary

        return {
            "invoices_processed": invoices_processed,
            "periods": list(periods_detected),
            "summaries": summaries_result,
        }

    @classmethod
    def calculate_monthly_summary(cls, business_id: str, period_year_month: str, db: Session) -> Dict[str, Any]:
        """Calcula el balance mensual de impuestos restando notas crédito y distinguiendo emitidos vs recibidos."""
        invoices = (
            db.query(Invoice)
            .filter(
                Invoice.business_id == business_id,
            )
            .all()
        )

        # Filtrar en memoria por period_year_month
        period_invoices = [inv for inv in invoices if inv.issue_date.strftime("%Y-%m") == period_year_month]

        total_invoiced_net = Decimal("0.00")
        iva_generado = Decimal("0.00")
        iva_descontable = Decimal("0.00")
        rete_iva_total = Decimal("0.00")
        rete_renta_total = Decimal("0.00")
        rete_ica_total = Decimal("0.00")

        for inv in period_invoices:
            doc_lower = inv.document_type.lower()
            is_credit_note = "crédito" in doc_lower or "credito" in doc_lower
            multiplier = Decimal("-1.00") if is_credit_note else Decimal("1.00")

            if inv.group_type == "Emitido":
                total_invoiced_net += inv.total * multiplier
                iva_generado += inv.iva * multiplier
            elif inv.group_type == "Recibido":
                iva_descontable += inv.iva * multiplier

            # Retenciones acumuladas
            rete_iva_total += inv.rete_iva * multiplier
            rete_renta_total += inv.rete_renta * multiplier
            rete_ica_total += inv.rete_ica * multiplier

        iva_balance = iva_generado - iva_descontable

        # Buscar resumen del mes anterior para calcular variación %
        parts = period_year_month.split("-")
        y, m = int(parts[0]), int(parts[1])
        if m == 1:
            prev_ym = f"{y-1}-12"
        else:
            prev_ym = f"{y}-{m-1:02d}"

        prev_summary = (
            db.query(MonthlyTaxSummary)
            .filter(
                MonthlyTaxSummary.business_id == business_id,
                MonthlyTaxSummary.period_year_month == prev_ym,
            )
            .first()
        )

        variation_pct = None
        if prev_summary and prev_summary.total_invoiced_net > 0:
            diff = total_invoiced_net - prev_summary.total_invoiced_net
            variation_pct = (diff / prev_summary.total_invoiced_net * Decimal("100.00")).quantize(Decimal("0.01"))

        # Upsert en MonthlyTaxSummary
        existing_summary = (
            db.query(MonthlyTaxSummary)
            .filter(
                MonthlyTaxSummary.business_id == business_id,
                MonthlyTaxSummary.period_year_month == period_year_month,
            )
            .first()
        )

        if existing_summary:
            existing_summary.total_invoiced_net = total_invoiced_net
            existing_summary.iva_generado = iva_generado
            existing_summary.iva_descontable = iva_descontable
            existing_summary.iva_balance = iva_balance
            existing_summary.rete_iva_total = rete_iva_total
            existing_summary.rete_renta_total = rete_renta_total
            existing_summary.rete_ica_total = rete_ica_total
            existing_summary.total_invoices_count = len(period_invoices)
            existing_summary.variation_vs_previous_pct = variation_pct
            existing_summary.calculated_at = datetime.utcnow()
        else:
            new_summary = MonthlyTaxSummary(
                business_id=business_id,
                period_year_month=period_year_month,
                total_invoiced_net=total_invoiced_net,
                iva_generado=iva_generado,
                iva_descontable=iva_descontable,
                iva_balance=iva_balance,
                rete_iva_total=rete_iva_total,
                rete_renta_total=rete_renta_total,
                rete_ica_total=rete_ica_total,
                total_invoices_count=len(period_invoices),
                variation_vs_previous_pct=variation_pct,
            )
            db.add(new_summary)

        db.commit()

        return {
            "period": period_year_month,
            "total_invoiced_net": total_invoiced_net,
            "iva_generado": iva_generado,
            "iva_descontable": iva_descontable,
            "iva_balance": iva_balance,
            "rete_iva_total": rete_iva_total,
            "rete_renta_total": rete_renta_total,
            "rete_ica_total": rete_ica_total,
            "total_invoices_count": len(period_invoices),
            "variation_vs_previous_pct": variation_pct,
        }

    @classmethod
    def parse_zip(
        cls,
        zip_path: str,
        business_id: str,
        db: Session,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Punto de entrada principal: descomprime y parsea el archivo ZIP de la DIAN."""
        xlsx_bytes = cls.extract_xlsx_bytes_from_zip(zip_path)
        return cls.parse_workbook(xlsx_bytes=xlsx_bytes, business_id=business_id, db=db, job_id=job_id)
