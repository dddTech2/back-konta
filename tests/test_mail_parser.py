import pytest
from datetime import datetime
from dian_automation import extract_token_url, parse_date_range

SAMPLE_DIAN_EMAIL_HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
<p>Estimado(a) gerente@example.com,</p>
<p>Se ha generado una nueva solicitud de acceso al Sistema de Factura Electrónica.</p>
<p>Acceda a la plataforma dando clic en el siguiente link generado:</p>
<p>
  <a href="https://catalogo-vpfe-hab.dian.gov.co/User/AuthToken?token=98765432-abcd-ef01-2345-6789abcdef01">
    <img src="https://catalogo-vpfe-hab.dian.gov.co/Content/Images/ingreso.png" alt="ingreso" />
  </a>
</p>
<p>Saludos Cordiales,</p>
</body>
</html>
"""

def test_extract_token_url_success():
    url = extract_token_url(SAMPLE_DIAN_EMAIL_HTML)
    assert url is not None
    assert "catalogo-vpfe-hab.dian.gov.co" in url
    assert "token=98765432-abcd-ef01-2345-6789abcdef01" in url

def test_extract_token_url_plain_regex():
    text_sample = "Por favor ingrese en https://catalogo-vpfe-hab.dian.gov.co/User/AuthToken?token=xyz123 para continuar."
    url = extract_token_url(text_sample)
    assert url == "https://catalogo-vpfe-hab.dian.gov.co/User/AuthToken?token=xyz123"

def test_extract_token_url_empty_or_missing():
    assert extract_token_url("") is None
    assert extract_token_url("Este correo no tiene enlaces.") is None
    assert extract_token_url("<html><body><a href='https://google.com'>Google</a></body></html>") is None

def test_parse_date_range():
    # Formato con barras
    res1 = parse_date_range("08/08/2026 - 08/09/2026")
    assert res1 is not None
    d1, d2 = res1
    assert d1 == datetime(2026, 8, 8)
    assert d2 == datetime(2026, 9, 8)

    # Formato con guiones
    res2 = parse_date_range("01-07-2026 - 31-07-2026")
    assert res2 is not None
    d3, d4 = res2
    assert d3 == datetime(2026, 7, 1)
    assert d4 == datetime(2026, 7, 31)

    # Formato inválido
    assert parse_date_range("") is None
    assert parse_date_range("rango invalido") is None

def test_wait_for_token_timeout(monkeypatch):
    from unittest.mock import MagicMock
    from dian_automation import StalwartMailClient

    client = StalwartMailClient("test.host", 993, "user", "pass")
    # Mockear IMAP para simular bandeja vacía
    mock_imap = MagicMock()
    mock_imap.select.return_value = ("OK", [b"1"])
    mock_imap.search.return_value = ("OK", [b""])
    client._client = mock_imap

    with pytest.raises(TimeoutError) as exc_info:
        # Timeout muy corto para el test (0.1s)
        client.wait_for_token_email(min_timestamp=datetime.now(), timeout_seconds=0.1, poll_interval=0.05)
    assert "No se recibió el correo con el token" in str(exc_info.value)

