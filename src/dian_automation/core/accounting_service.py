"""Servicio de Contabilidad y Reparto de Ingresos Visión Contable / NYO (Story 10.3).

Gestiona los porcentajes de reparto comercial por paquete, congelamiento de montos
por pago y consolidación del informe contable (recaudo, reparto, clientes, MRR y evolución mensual).
"""

import calendar
import logging
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from sqlalchemy.orm import Session

from dian_automation.core.business_profile import package_of
from dian_automation.db.models import (
    Business,
    PaymentRecord,
    RevenueSplitSetting,
    Subscription,
    User,
    INCOME_SOURCE_DIAN,
    INCOME_SOURCE_MANUAL_SALES,
    INCOME_SOURCE_MIXED,
    PACKAGE_CON_IVA,
    PACKAGE_SIN_IVA,
    PACKAGES,
)
from dian_automation.subscriptions.service import add_months_to_date

logger = logging.getLogger("accounting_service")


class AccountingError(Exception):
    """Excepción de dominio para contabilidad y reparto con código tipado y status HTTP."""

    def __init__(self, code: str, message: str = "", status_code: int = 400):
        if isinstance(message, int):
            status_code = message
            message = code
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status_code = status_code


def split_amount(amount: Any, nyo_pct: Any) -> Tuple[Decimal, Decimal]:
    """Calcula el reparto entre NYO y Visión Contable a pesos enteros (ROUND_HALF_UP).

    nyo = round(amount * nyo_pct / 100) a pesos enteros
    visioncontable = amount - nyo
    Las dos partes siempre suman el monto pagado.
    """
    amount_dec = Decimal(str(amount))
    pct_dec = Decimal(str(nyo_pct))
    nyo = (amount_dec * pct_dec / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    visioncontable = amount_dec - nyo
    return nyo, visioncontable


def ensure_default_split(db: Session) -> None:
    """Garantiza la presencia de la semilla de reparto (50/50 para CON_IVA y SIN_IVA).

    Solo hace flush (nunca commit), igual que ensure_default_pricing.
    """
    seeded = False
    existing_packages = {
        row[0] for row in db.query(RevenueSplitSetting.package).all()
    }
    now_dt = datetime.utcnow()
    for pkg in PACKAGES:
        if pkg not in existing_packages:
            seeded = True
            db.add(
                RevenueSplitSetting(
                    package=pkg,
                    nyo_pct=Decimal("50.00"),
                    visioncontable_pct=Decimal("50.00"),
                    updated_at=now_dt,
                    updated_by_admin_id=None,
                )
            )
    if seeded:
        db.flush()


def get_split(db: Session) -> List[RevenueSplitSetting]:
    """Obtiene la lista de configuraciones de reparto por paquete."""
    ensure_default_split(db)
    return (
        db.query(RevenueSplitSetting)
        .order_by(RevenueSplitSetting.package.asc())
        .all()
    )


def get_split_setting(db: Session, package: str) -> RevenueSplitSetting:
    """Obtiene la configuración de reparto vigente para un paquete específico."""
    ensure_default_split(db)
    pkg_clean = (package or "").strip().upper()
    setting = (
        db.query(RevenueSplitSetting)
        .filter(RevenueSplitSetting.package == pkg_clean)
        .first()
    )
    if not setting:
        raise AccountingError("INVALID_PACKAGE", f"Paquete '{package}' no válido.", 404)
    return setting


def set_split(
    db: Session,
    package: str,
    nyo_pct: Any,
    admin: Optional[User] = None,
) -> RevenueSplitSetting:
    """Actualiza el porcentaje de reparto de un paquete.

    Valida que package pertenezca a PACKAGES y que nyo_pct esté entre 0 y 100 con hasta 2 decimales.
    Calcula visioncontable_pct = 100 - nyo_pct.
    Solo hace flush (nunca commit).
    """
    pkg_clean = (package or "").strip().upper()
    if pkg_clean not in PACKAGES:
        raise AccountingError(
            "INVALID_PACKAGE",
            f"Paquete '{package}' no válido. Opciones: {', '.join(PACKAGES)}",
            400,
        )

    try:
        pct = Decimal(str(nyo_pct))
    except Exception:
        raise AccountingError("INVALID_SPLIT", f"Porcentaje '{nyo_pct}' inválido.", 400)

    if pct < Decimal("0") or pct > Decimal("100"):
        raise AccountingError("INVALID_SPLIT", "El porcentaje debe estar entre 0 y 100.", 400)

    if pct.quantize(Decimal("0.01")) != pct:
        raise AccountingError("INVALID_SPLIT", "El porcentaje no puede tener más de 2 decimales.", 400)

    pct_2dec = pct.quantize(Decimal("0.01"))
    vc_pct = Decimal("100.00") - pct_2dec

    ensure_default_split(db)
    setting = (
        db.query(RevenueSplitSetting)
        .filter(RevenueSplitSetting.package == pkg_clean)
        .first()
    )
    admin_id = admin.id if admin else None
    if not setting:
        setting = RevenueSplitSetting(
            package=pkg_clean,
            nyo_pct=pct_2dec,
            visioncontable_pct=vc_pct,
            updated_at=datetime.utcnow(),
            updated_by_admin_id=admin_id,
        )
        db.add(setting)
    else:
        setting.nyo_pct = pct_2dec
        setting.visioncontable_pct = vc_pct
        setting.updated_at = datetime.utcnow()
        setting.updated_by_admin_id = admin_id

    db.flush()
    return setting


def accounting_summary(
    db: Session,
    year: int,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    """Calcula el resumen contable y de reparto para un año o un mes específico.

    Retorna un diccionario con las claves exactas del AC #7:
    collected, split, clients, mrr, mrr_split, monthly, payments.
    """
    if month is not None:
        if not (1 <= month <= 12):
            raise AccountingError("INVALID_PERIOD", f"Mes '{month}' fuera de rango (1-12).", 400)
        start_date = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        end_date = date(year, month, last_day)
    else:
        start_date = date(year, 1, 1)
        end_date = date(year, 12, 31)

    # 1. Pagos del periodo filtrados por PaymentRecord.payment_date
    period_payments = (
        db.query(PaymentRecord)
        .filter(
            PaymentRecord.payment_date >= start_date,
            PaymentRecord.payment_date <= end_date,
        )
        .all()
    )

    # 2. collected
    collected_total = Decimal("0.00")
    by_package_collected = {
        PACKAGE_CON_IVA: {"total": Decimal("0.00"), "count": 0},
        PACKAGE_SIN_IVA: {"total": Decimal("0.00"), "count": 0},
    }
    for p in period_payments:
        pkg = p.package if p.package in PACKAGES else PACKAGE_SIN_IVA
        amt = Decimal(str(p.amount))
        collected_total += amt
        by_package_collected[pkg]["total"] += amt
        by_package_collected[pkg]["count"] += 1

    collected = {
        "total": collected_total,
        "count": len(period_payments),
        "by_package": by_package_collected,
    }

    # 3. split (sumando montos congelados de pagos del periodo)
    nyo_total = Decimal("0.00")
    nyo_by_package = {PACKAGE_CON_IVA: Decimal("0.00"), PACKAGE_SIN_IVA: Decimal("0.00")}
    vc_total = Decimal("0.00")
    vc_by_package = {PACKAGE_CON_IVA: Decimal("0.00"), PACKAGE_SIN_IVA: Decimal("0.00")}

    for p in period_payments:
        pkg = p.package if p.package in PACKAGES else PACKAGE_SIN_IVA
        nyo_amt = Decimal(str(p.nyo_amount)) if p.nyo_amount is not None else Decimal("0.00")
        vc_amt = Decimal(str(p.visioncontable_amount)) if p.visioncontable_amount is not None else Decimal("0.00")

        nyo_total += nyo_amt
        nyo_by_package[pkg] += nyo_amt
        vc_total += vc_amt
        vc_by_package[pkg] += vc_amt

    split = {
        "nyo": {
            "total": nyo_total,
            "by_package": nyo_by_package,
        },
        "visioncontable": {
            "total": vc_total,
            "by_package": vc_by_package,
        },
    }

    # 4. clients y mrr (reutilizando patrón de get_admin_summary)
    latest_sub_id = (
        db.query(Subscription.id)
        .filter(Subscription.client_id == Business.client_id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(1)
        .correlate(Business)
        .scalar_subquery()
    )

    businesses = (
        db.query(Business, Subscription)
        .join(User, Business.client_id == User.id)
        .outerjoin(Subscription, Subscription.id == latest_sub_id)
        .filter(User.role == "CLIENT")
        .all()
    )

    active_total = 0
    clients_by_package = {PACKAGE_CON_IVA: 0, PACKAGE_SIN_IVA: 0}
    clients_by_income_source = {
        INCOME_SOURCE_DIAN: 0,
        INCOME_SOURCE_MANUAL_SALES: 0,
        INCOME_SOURCE_MIXED: 0,
    }
    new_in_period = 0
    mrr_by_package = {PACKAGE_CON_IVA: Decimal("0.00"), PACKAGE_SIN_IVA: Decimal("0.00")}

    bogota_tz = ZoneInfo("America/Bogota")

    for biz, sub in businesses:
        # Negocios creados en el periodo (hora Bogotá)
        if biz.created_at:
            created_dt_utc = (
                biz.created_at
                if biz.created_at.tzinfo
                else biz.created_at.replace(tzinfo=ZoneInfo("UTC"))
            )
            created_date_bogota = created_dt_utc.astimezone(bogota_tz).date()
            if start_date <= created_date_bogota <= end_date:
                new_in_period += 1

        # Suscripción activa = sub existe y status distinto de BLOQUEADO y CANCELADO
        if sub and sub.status not in ("BLOQUEADO", "CANCELADO"):
            active_total += 1

            pkg = package_of(biz)
            if pkg in clients_by_package:
                clients_by_package[pkg] += 1
            else:
                clients_by_package[pkg] = 1

            if biz.income_source == INCOME_SOURCE_MANUAL_SALES:
                clients_by_income_source[INCOME_SOURCE_MANUAL_SALES] += 1
            elif biz.income_source == INCOME_SOURCE_MIXED:
                clients_by_income_source[INCOME_SOURCE_MIXED] += 1
            else:
                clients_by_income_source[INCOME_SOURCE_DIAN] += 1

            sub_pkg = sub.package or pkg
            sub_pkg = sub_pkg if sub_pkg in PACKAGES else PACKAGE_SIN_IVA
            monthly_price = Decimal(str(sub.monthly_price or 0))
            mrr_by_package[sub_pkg] += monthly_price

    clients = {
        "active_total": active_total,
        "by_package": clients_by_package,
        "by_income_source": clients_by_income_source,
        "new_in_period": new_in_period,
    }

    mrr = {
        "total": mrr_by_package[PACKAGE_CON_IVA] + mrr_by_package[PACKAGE_SIN_IVA],
        "by_package": mrr_by_package,
    }

    # 5. mrr_split (proyección mensual con el porcentaje vigente de cada paquete)
    split_settings_list = get_split(db)
    split_map = {row.package: row.nyo_pct for row in split_settings_list}
    con_iva_pct = split_map.get(PACKAGE_CON_IVA, Decimal("50.00"))
    sin_iva_pct = split_map.get(PACKAGE_SIN_IVA, Decimal("50.00"))

    nyo_mrr_con_iva, vc_mrr_con_iva = split_amount(mrr_by_package[PACKAGE_CON_IVA], con_iva_pct)
    nyo_mrr_sin_iva, vc_mrr_sin_iva = split_amount(mrr_by_package[PACKAGE_SIN_IVA], sin_iva_pct)

    mrr_split = {
        "nyo": {
            "total": nyo_mrr_con_iva + nyo_mrr_sin_iva,
            "by_package": {
                PACKAGE_CON_IVA: nyo_mrr_con_iva,
                PACKAGE_SIN_IVA: nyo_mrr_sin_iva,
            },
        },
        "visioncontable": {
            "total": vc_mrr_con_iva + vc_mrr_sin_iva,
            "by_package": {
                PACKAGE_CON_IVA: vc_mrr_con_iva,
                PACKAGE_SIN_IVA: vc_mrr_sin_iva,
            },
        },
    }

    # 6. monthly (últimos 12 meses hasta el periodo, [{month: "YYYY-MM", CON_IVA, SIN_IVA, total}])
    if month is not None:
        ref_start = date(year, month, 1)
        months_to_query = [add_months_to_date(ref_start, -i) for i in range(11, -1, -1)]
    else:
        months_to_query = [date(year, m, 1) for m in range(1, 13)]

    earliest_date = months_to_query[0]
    last_query_date = months_to_query[-1]
    latest_date = date(
        last_query_date.year,
        last_query_date.month,
        calendar.monthrange(last_query_date.year, last_query_date.month)[1],
    )

    monthly_payments = (
        db.query(PaymentRecord)
        .filter(
            PaymentRecord.payment_date >= earliest_date,
            PaymentRecord.payment_date <= latest_date,
        )
        .all()
    )

    monthly_dict = {
        f"{d.year:04d}-{d.month:02d}": {
            "month": f"{d.year:04d}-{d.month:02d}",
            PACKAGE_CON_IVA: Decimal("0.00"),
            PACKAGE_SIN_IVA: Decimal("0.00"),
            "total": Decimal("0.00"),
        }
        for d in months_to_query
    }

    for p in monthly_payments:
        if not p.payment_date:
            continue
        m_key = f"{p.payment_date.year:04d}-{p.payment_date.month:02d}"
        if m_key in monthly_dict:
            pkg = p.package if p.package in PACKAGES else PACKAGE_SIN_IVA
            amt = Decimal(str(p.amount))
            monthly_dict[m_key][pkg] += amt
            monthly_dict[m_key]["total"] += amt

    monthly = list(monthly_dict.values())

    # 7. payments (detalle del periodo, máx 200, más reciente primero)
    recent_payments = (
        db.query(PaymentRecord)
        .filter(
            PaymentRecord.payment_date >= start_date,
            PaymentRecord.payment_date <= end_date,
        )
        .order_by(
            PaymentRecord.payment_date.desc(),
            PaymentRecord.created_at.desc(),
            PaymentRecord.id.desc(),
        )
        .limit(200)
        .all()
    )

    sub_ids = [p.subscription_id for p in recent_payments if p.subscription_id]
    subs = db.query(Subscription).filter(Subscription.id.in_(sub_ids)).all() if sub_ids else []
    sub_map = {s.id: s for s in subs}
    client_ids = [s.client_id for s in subs if s.client_id]

    biz_rows = (
        db.query(Business)
        .filter(Business.client_id.in_(client_ids))
        .order_by(Business.is_active.desc(), Business.created_at.asc())
        .all()
        if client_ids
        else []
    )
    biz_map = {}
    for b in biz_rows:
        if b.client_id not in biz_map:
            biz_map[b.client_id] = b

    payments_detail = []
    for p in recent_payments:
        sub = sub_map.get(p.subscription_id)
        biz = biz_map.get(sub.client_id) if sub else None
        pkg = p.package if p.package in PACKAGES else (package_of(biz) if biz else PACKAGE_SIN_IVA)

        payments_detail.append({
            "payment_date": p.payment_date,
            "business_id": biz.id if biz else "",
            "commercial_name": biz.commercial_name if biz else "",
            "package": pkg,
            "amount": Decimal(str(p.amount)),
            "nyo_amount": Decimal(str(p.nyo_amount)) if p.nyo_amount is not None else Decimal("0.00"),
            "visioncontable_amount": (
                Decimal(str(p.visioncontable_amount))
                if p.visioncontable_amount is not None
                else Decimal("0.00")
            ),
        })

    return {
        "collected": collected,
        "split": split,
        "clients": clients,
        "mrr": mrr,
        "mrr_split": mrr_split,
        "monthly": monthly,
        "payments": payments_detail,
    }
