import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import re
import time
from datetime import datetime, timezone
from typing import Optional
from html.parser import HTMLParser

class TokenLinkParser(HTMLParser):
    """Parser HTML para encontrar el enlace envuelto en la imagen de ingreso o enlace con token DIAN."""
    def __init__(self):
        super().__init__()
        self.found_url: Optional[str] = None
        self._current_href: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag.lower() == "a":
            href = attrs_dict.get("href")
            if href:
                self._current_href = href
                # Si el href contiene directamente catalogo-vpfe y token, es el objetivo
                if "catalogo-vpfe" in href.lower() and ("token" in href.lower() or "auth" in href.lower()):
                    self.found_url = href
        elif tag.lower() == "img" and self._current_href:
            # Comprobar si la imagen tiene relación con ingreso
            alt_or_src = (attrs_dict.get("alt", "") + " " + attrs_dict.get("src", "")).lower()
            if "ingreso" in alt_or_src or "login" in alt_or_src or "dian" in alt_or_src:
                self.found_url = self._current_href

    def handle_endtag(self, tag):
        if tag.lower() == "a":
            self._current_href = None

def extract_token_url(html_content: str) -> Optional[str]:
    """
    Extrae la URL del token de acceso a la DIAN desde el cuerpo HTML del correo.
    Busca mediante el parser de etiquetas HTML y como fallback mediante expresiones regulares.
    """
    if not html_content:
        return None

    # Intento 1: Parser de árbol HTML buscando tag <a> con imagen o enlace directo
    try:
        parser = TokenLinkParser()
        parser.feed(html_content)
        if parser.found_url:
            return parser.found_url
    except Exception:
        pass

    # Intento 2: Regex buscando URL que coincida con el dominio y estructura del portal DIAN
    # Ej: https://catalogo-vpfe-hab.dian.gov.co/User/AuthToken?token=...
    match = re.search(
        r'https?://[a-zA-Z0-9.-]*dian\.gov\.co/[^\s"\'<>]+(?:token|AuthToken|TokenAuth)[^\s"\'<>]*',
        html_content,
        re.IGNORECASE
    )
    if match:
        return match.group(0)

    # Intento 3: Cualquier URL dentro de un <a href="..."> cercana al texto "ingreso"
    match_fallback = re.search(
        r'<a\s+[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>[\s\S]*?(?:ingreso|<img[^>]+(?:ingreso|dian)[\s\S]*?>)[\s\S]*?</a>',
        html_content,
        re.IGNORECASE
    )
    if match_fallback:
        return match_fallback.group(1)

    return None

def _decode_str(header_value: str) -> str:
    """Decodifica encabezados MIME que puedan venir codificados."""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)

def _get_email_body(msg: email.message.Message) -> str:
    """Extrae el cuerpo HTML o texto plano del mensaje de correo."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    if content_type == "text/html":
                        return text  # Preferir HTML directamente
                    body_parts.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return "\n".join(body_parts)

class StalwartMailClient:
    """Cliente para interactuar con Stalwart Mail Server vía IMAP SSL."""
    def __init__(self, host: str, port: int, user: str, password: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self._client: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> None:
        if not self.password:
            raise ValueError("STALWART_PASSWORD no está configurada. Por favor configúrala en el archivo .env")
        self._client = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        self._client.login(self.user, self.password)

    def disconnect(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            try:
                self._client.logout()
            except Exception:
                pass
            self._client = None

    def wait_for_token_email(
        self,
        min_timestamp: datetime,
        timeout_seconds: int = 60,
        poll_interval: float = 2.0
    ) -> str:
        """
        Realiza polling en la bandeja de entrada buscando un correo de la DIAN recibido
        después de min_timestamp que contenga el enlace de acceso.
        """
        if not self._client:
            self.connect()

        start_time = time.time()
        # Asegurar que min_timestamp tenga zona horaria UTC para comparación consistente
        if min_timestamp.tzinfo is None:
            min_timestamp = min_timestamp.replace(tzinfo=timezone.utc)

        mailboxes_to_check = ['INBOX', '"Junk Mail"', 'Spam', 'Junk']

        while (time.time() - start_time) < timeout_seconds:
            for mailbox in mailboxes_to_check:
                try:
                    status, _ = self._client.select(mailbox)
                    if status != "OK":
                        continue
                except Exception:
                    continue

                status, message_numbers = self._client.search(None, "ALL")
                if status == "OK" and message_numbers[0]:
                    msg_ids = message_numbers[0].split()
                    # Revisar desde el más reciente hacia atrás
                    for msg_id in reversed(msg_ids[-10:]):
                        status, data = self._client.fetch(msg_id, "(RFC822)")
                        if status != "OK" or not data or not data[0]:
                            continue
                        
                        raw_email = data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        
                        # Validar fecha del correo
                        date_header = msg.get("Date")
                        if date_header:
                            try:
                                msg_date = parsedate_to_datetime(date_header)
                                if msg_date.tzinfo is None:
                                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                                # Permitir un margen de 120 segundos de desfase de reloj del servidor
                                if (min_timestamp - msg_date).total_seconds() > 120:
                                    continue
                            except Exception:
                                pass

                        subject = _decode_str(msg.get("Subject", ""))
                        body = _get_email_body(msg)
                        if (
                            "Token" in subject
                            or "DIAN" in subject
                            or "Factura Electrónica" in subject
                            or "catalogo-vpfe" in body
                            or "solicitud de acceso" in body
                        ):
                            token_url = extract_token_url(body)
                            if token_url:
                                return token_url

            time.sleep(poll_interval)

        raise TimeoutError(
            f"No se recibió el correo con el token de la DIAN en Stalwart dentro de los {timeout_seconds} segundos."
        )
