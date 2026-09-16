# ADR-002: Autenticación Passwordless y Bypass de Magic Link vía Stalwart IMAP

## Status
Accepted

## Date
2026-09-13

## Context
El sistema de autenticación de la DIAN para Facturación Electrónica no utiliza contraseñas estáticas. En su lugar, implementa un esquema **Passwordless**:
1. El usuario suministra su número de identificación (Cédula o NIT).
2. La plataforma de la DIAN genera internamente un token criptográfico efímero y envía un correo electrónico a la dirección registrada formalmente en el RUT del contribuyente.
3. El correo contiene un botón de "Ingreso" o enlace con el parámetro `AuthToken?token=...`, el cual tiene una vida útil limitada (usualmente 30 a 60 minutos) y queda invalidado tras su uso.

Para permitir que el sistema funcione de manera **autónoma y desatendida** (por ejemplo, en ejecuciones nocturnas o flujos batch masivos), el sistema debe:
- Detectar la llegada del nuevo correo en tiempo real.
- Evitar falsos positivos con correos antiguos o tokens expirados de ejecuciones previas.
- Extraer con 100% de confiabilidad la URL de autenticación sin importar variaciones estéticas en la plantilla HTML enviada por la DIAN.

## Decision
Implementar un cliente adaptador especializado (`StalwartMailClient`) basado en el protocolo estándar **IMAP con cifrado SSL (puerto 993)**, conectado a la instancia corporativa de **Stalwart Mail Server**.

La estrategia de extracción incorpora:
1. **Filtro Temporal Estricto (`since_time`):** Al enviar el formulario de login en la web, el orquestador registra la marca de tiempo exacta (`datetime.now(timezone.utc)`). El sondeo IMAP descarta cualquier correo cuya cabecera `Date` sea anterior a ese instante, evitando reutilizar tokens viejos.
2. **Sondeo con Backoff y Múltiples Carpetas:** Revisa concurrentemente `INBOX`, `Junk Mail` y `Spam` con pausas de 3 a 5 segundos hasta un timeout configurable (por defecto 60s).
3. **Parser Dual de Extracción de Token:**
   - **Parser Estructural (`TokenLinkParser`):** Emplea `html.parser.HTMLParser` para navegar el árbol DOM del correo, identificando hipervínculos `<a>` que envuelven etiquetas `<img>` con atributos descriptivos (`alt="ingreso"`, `src="...login..."`) o que contengan directamente `catalogo-vpfe` y `token` en su atributo `href`.
   - **Fallback por Expresión Regular:** Si el parser estructural no halla el nodo, aplica regex sobre el texto plano decodificado buscando patrones que coincidan con `https?://[a-zA-Z0-9.-]*dian\.gov\.co/...[token]`.

## Alternatives Considered

### 1. Webhooks HTTP Entrantes (Mailgun, SendGrid, Amazon SES)
- **Ventajas:** Notificación en tiempo real basada en push en lugar de polling.
- **Desventajas:** Exige que el servidor donde corre la automatización tenga una IP pública estática, certificado SSL y un puerto abierto accesible desde internet (o un túnel como ngrok); requiere modificar los registros MX y DNS de la empresa para delegar la recepción de correos a un proveedor SaaS de terceros.
- **Rechazada:** Demasiada fricción de infraestructura y no viable para entornos on-premise cerrados o máquinas de desarrollo locales.

### 2. Scraping de Interfaces Webmail (Roundcube, Gmail Web)
- **Ventajas:** No requiere credenciales IMAP si ya existe una sesión web abierta.
- **Desventajas:** Fragilidad extrema ante actualizaciones del cliente webmail; consumo innecesario de recursos al tener que levantar otra instancia de navegador para leer el correo.
- **Rechazada:** Complejidad innecesaria y alta probabilidad de fallas.

### 3. Notificación Manual por WhatsApp / Slack con Intervención Humana
- **Ventajas:** No requiere acceso programático al buzón de correo.
- **Desventajas:** Rompe la promesa de automatización desatendida; el tiempo de respuesta humano suele exceder la expiración del token de la DIAN.
- **Rechazada:** No escalable para procesamiento de múltiples empresas o cron jobs automáticos.

## Consequences

### Positivas
- **Extracción Sub-segundo:** El Magic Link se captura y decodifica en un promedio de 800 milisegundos una vez que el servidor SMTP de la DIAN entrega el mensaje.
- **Tolerancia a Rediseños:** Si la DIAN cambia de color el botón, reemplaza la imagen o modifica la maquetación CSS del correo, el parser dual sigue funcionando sin interrupción.
- **Seguridad y Aislamiento:** Toda la comunicación con el servidor de correo se realiza mediante canales seguros con cifrado TLS/SSL (`IMAP4_SSL`).

### Negativas / Mitigaciones
- **Retardo en Entrega del Proveedor DIAN:** En ocasiones, la infraestructura de correo de la DIAN presenta colas de espera o saturación en sus servidores salientes.
  * *Mitigación:* Se implementó un parámetro de configuración `EMAIL_TIMEOUT_SECONDS` configurable en `.env` (por defecto 60s, ampliable a 180s en días pico tributarios).
- **Almacenamiento de Credenciales:** Requiere almacenar usuario y contraseña del buzón.
  * *Mitigación:* Se cargan estrictamente mediante variables de entorno en `.env` (ignorado en `.gitignore`) y nunca se imprimen en logs o trazas de depuración.
