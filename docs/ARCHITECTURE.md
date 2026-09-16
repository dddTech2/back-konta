# Arquitectura del Sistema: DIAN VPFE Automation

Este documento describe la arquitectura de software del sistema de automatización para la descarga de listados de facturación electrónica desde el portal **DIAN VPFE** (Validación Previa de Facturación Electrónica), implementado con **Python**, **Playwright** y **Stalwart IMAP**.

La documentación sigue el estándar del **Modelo C4** (*Context*, *Containers*, *Components*) con diagramas interactivos en sintaxis **Mermaid**.

---

## 1. C4 Nivel 1: Diagrama de Contexto del Sistema (System Context)

El diagrama de contexto muestra el sistema de automatización en relación con los usuarios humanos (contadores/operadores) y los sistemas externos con los que interactúa.

```mermaid
C4Context
  title C4 Nivel 1: Contexto del Sistema - DIAN Automation

  Person(user, "Contador / Operador / Cron", "Ejecuta la extracción de listados de documentos electrónicos o programa la tarea periódica.")
  
  System(dianApp, "DIAN Automation System", "Automatiza el ingreso por Magic Link, navegación, solicitud de exportación y descarga de archivos Excel/ZIP de la DIAN.")
  
  System_Ext(dianPortal, "Portal DIAN VPFE", "Catálogo institucional de Facturación Electrónica (catalogo-vpfe.dian.gov.co).")
  System_Ext(mailServer, "Servidor de Correo Stalwart", "Servidor IMAP corporativo (mail.example.com) que recibe los tokens OTP / Magic Links de acceso.")
  SystemDb_Ext(fileSystem, "Almacenamiento Local", "Sistema de archivos local donde se persisten los listados descargados en ./downloads.")

  Rel(user, dianApp, "Ejecuta comandos CLI", "Terminal / Bash")
  Rel(dianApp, dianPortal, "Automatiza navegación, login y solicitudes de exportación", "HTTPS / Chromium")
  Rel(dianPortal, mailServer, "Envía correo con token de acceso dinámico", "SMTP")
  Rel(dianApp, mailServer, "Sondea buzón y extrae URL del token", "IMAPS (Puerto 993)")
  Rel(dianApp, fileSystem, "Guarda reportes descargados (ZIP/Excel)", "File I/O")
```

### Elementos del Contexto
* **DIAN Automation System**: Núcleo del aplicativo que orquesta la autenticación desatendida mediante lectura en tiempo real del correo, resolución de selectores y polling de la cola de generación de la DIAN.
* **Portal DIAN VPFE**: Aplicación web externa del estado colombiano que protege el acceso mediante tokens de un solo uso enviados al correo registrado en el RUT.
* **Servidor de Correo Stalwart**: Servidor de mensajería empresarial que almacena los correos transaccionales de la DIAN para su lectura inmediata.

---

## 2. C4 Nivel 2: Diagrama de Contenedores (Container Diagram)

Muestra los bloques de construcción ejecutables y entornos que componen el aplicativo.

```mermaid
C4Container
  title C4 Nivel 2: Contenedores - DIAN Automation System

  Person(user, "Operador / Scheduler", "Usuario o tarea programada")

  Container_Boundary(dianApp, "DIAN Automation Application") {
    Container(cli, "CLI Entrypoint", "Python (dian_automation.py)", "Parsea argumentos de línea de comandos (tipo de login, fechas, headless mode).")
    Container(config, "Config & Environment", "Python dataclasses, dotenv", "Carga variables de entorno, credenciales y mapeo centralizado de selectores DOM.")
    Container(mailClient, "Mail Client Adapter", "Python imaplib, HTMLParser", "Conexión IMAP segura, polling con filtro temporal y extracción de tokens mediante árbol HTML y Regex.")
    Container(automationEngine, "Automation Engine", "Playwright Async, Playwright Stealth", "Control del ciclo de vida del navegador, inyección de scripts anti-detección, navegación e interacción con modales.")
  }

  Container_Ext(chromium, "Browser Engine (Chromium)", "Playwright Subprocess", "Instancia aislada del navegador web controlada por Playwright.")
  System_Ext(dianPortal, "Portal DIAN VPFE", "Servicio web DIAN")
  System_Ext(mailServer, "Stalwart Mail Server", "IMAPS :993")
  SystemDb_Ext(storage, "Descargas Locales", "Carpeta ./downloads")

  Rel(user, cli, "Invoca ejecución", "CLI")
  Rel(cli, config, "Lee configuración y selectores")
  Rel(cli, automationEngine, "Inicia orquestación del flujo")
  Rel(automationEngine, mailClient, "Solicita extracción del Magic Link")
  Rel(mailClient, mailServer, "Sondea correos recientes", "IMAPS / SSL")
  Rel(automationEngine, chromium, "Envía comandos CDP / Playwright", "WebSocket / IPC")
  Rel(chromium, dianPortal, "Navegación e interacción DOM", "HTTPS")
  Rel(automationEngine, storage, "Escribe archivos descargados", "POSIX File I/O")
```

> 📋 **Variables de Entorno:** Para consultar el inventario completo de variables consumidas por el contenedor de configuración, sus valores por defecto y estado en `.env.example`, consulta [docs/ENVIRONMENT.md](ENVIRONMENT.md).

---

## 3. C4 Nivel 3: Diagrama de Componentes (Component Diagram)

Profundización dentro del **Automation Engine** y del **Mail Client Adapter** para detallar la lógica interna de los módulos de Python.

```mermaid
C4Component
  title C4 Nivel 3: Componentes Internos de Automatización

  Container_Boundary(engineBoundary, "src/dian_automation") {
    Component(dianFlow, "DianFlow Orquestador", "dian_flow.py", "Coordina las fases del proceso: inicio de sesión, espera de correo, navegación y descarga.")
    Component(sanitizer, "NIT / Date Sanitizer", "dian_flow.py (helpers)", "Limpia y valida NITs (remueve DV) y formatea rangos de fechas compatibles con el selector DIAN.")
    Component(mailPoller, "StalwartMailClient", "mail_client.py", "Conecta con IMAP, calcula ventanas de tiempo (since_time) y recupera el último mensaje no leído de la DIAN.")
    Component(htmlParser, "TokenLinkParser & Regex", "mail_client.py", "Parsea etiquetas <a> e <img> buscando enlaces envueltos en botones de login o parámetros de token.")
    Component(tablePoller, "TableExportPoller", "dian_flow.py", "Sondea la tabla #tableExport, refresca el estado y detecta cuando el reporte pasa a estado 'Listo'.")
    Component(selectors, "DianSelectors", "config.py", "Constantes inmutables de selectores CSS y XPath para campos de entrada, botones y modales.")
  }

  Container_Ext(playwrightPage, "Playwright Page Context", "Browser Tab Context")
  System_Ext(mailHost, "Stalwart IMAP", "Mail Server")

  Rel(dianFlow, selectors, "Consulta selectores")
  Rel(dianFlow, sanitizer, "Valida inputs de usuario")
  Rel(dianFlow, playwrightPage, "Interactúa con página web")
  Rel(dianFlow, mailPoller, "Invoca get_magic_link()")
  Rel(mailPoller, mailHost, "FETCH RFC822", "IMAP")
  Rel(mailPoller, htmlParser, "Entrega cuerpo HTML del correo")
  Rel(dianFlow, tablePoller, "Monitorea generación de reporte")
  Rel(tablePoller, playwrightPage, "Verifica #tableExport y activa descarga")
```

---

## 4. Diagrama de Secuencia Dinámico (End-to-End Workflow)

Este diagrama de interacción temporal muestra cómo se comunican los componentes durante una ejecución típica completa:

```mermaid
sequenceDiagram
    autonumber
    actor CLI as Usuario / Runner
    participant Flow as dian_flow (Orquestador)
    participant Chrome as Playwright (Browser)
    participant Portal as Portal DIAN VPFE
    participant Mail as Stalwart IMAP
    participant Disk as Disco Local (downloads/)

    CLI->>Flow: Ejecutar descarga (tipo: persona/empresa, rango fechas)
    Flow->>Chrome: Inicializar contexto con Playwright-Stealth
    Flow->>Chrome: Abrir página de login DIAN
    Chrome->>Portal: GET /User/PersonLogin o CompanyLogin
    Flow->>Chrome: Llenar documento/NIT y presionar "Entrar"
    Chrome->>Portal: POST credenciales iniciales
    Portal-->>Chrome: Mensaje: "Se ha enviado un enlace a su correo"

    Note over Flow,Mail: Inicio de sondeo del Magic Link
    loop Sondeo con backoff (Timeout 60s)
        Flow->>Mail: Buscar correo nuevo de la DIAN (since_time)
        alt Correo recibido
            Mail-->>Flow: Retornar cuerpo del correo
            Flow->>Flow: Extraer URL con TokenLinkParser
        else Aún no recibido
            Flow->>Flow: Esperar intervalo (3s - 5s)
        end
    end

    Note over Flow,Portal: Autenticación con Token
    Flow->>Chrome: Navegar directamente a URL del Token
    Chrome->>Portal: GET /User/AuthToken?token=...
    Portal-->>Chrome: 200 OK (Sesión autenticada en el portal)

    Note over Flow,Portal: Navegación y Solicitud de Reporte
    Flow->>Chrome: Navegar a "Histórico" -> "Descarga de listados"
    Flow->>Chrome: Configurar fechas (#export-range) y presionar "Exportar Excel"
    Chrome->>Portal: Solicitar generación de listado
    Flow->>Chrome: Confirmar modal (#confirmModal-confirm-button)

    Note over Flow,Portal: Monitoreo de Generación Asíncrona
    loop Polling de la tabla #tableExport
        Flow->>Chrome: Inspeccionar estado de fila más reciente
        alt Estado == "Listo" (i.fa-check)
            Flow->>Chrome: Clic en enlace de descarga (.fa-download)
        else Estado == "En proceso"
            Flow->>Chrome: Recargar / esperar actualización
        end
    end

    Chrome->>Portal: Descargar archivo ZIP
    Portal-->>Chrome: Stream binario del archivo
    Chrome->>Disk: Guardar archivo en ./downloads/[NIT]_[Fecha].zip
    Flow-->>CLI: Proceso finalizado con éxito
```

---

## 5. Resumen de Decisiones de Arquitectura Clave (ADRs)

Para conocer el análisis detallado, contexto de requisitos y alternativas descartadas de cada decisión, consulta el directorio de [Registros de Decisiones de Arquitectura (docs/decisions/)](decisions/README.md):

* **[ADR-001: Evasión de Cloudflare Turnstile mediante Chrome Nativo y CDP](decisions/ADR-001-evasion-cloudflare-turnstile-cdp.md)**: Justificación técnica del uso de Chrome oficial vía CDP sobre navegadores sintéticos o servicios de captcha externos.
* **[ADR-002: Autenticación Passwordless y Bypass de Magic Link vía Stalwart IMAP](decisions/ADR-002-autenticacion-passwordless-imap-stalwart.md)**: Justificación del sondeo IMAP con filtro `since_time` y parser dual (HTML + Regex) frente a webhooks HTTP o scrapers de webmail.

| Decisión | Justificación Técnica | ADR Asociado |
| :--- | :--- | :--- |
| **Chrome Nativo + CDP** | Evasión limpia y gratuita de Cloudflare Turnstile en 3-5 segundos sin captchas externos. | [ADR-001](decisions/ADR-001-evasion-cloudflare-turnstile-cdp.md) |
| **Bypass IMAP con Stalwart** | Detección en tiempo real de correos de login con tolerancia a rediseños visuales. | [ADR-002](decisions/ADR-002-autenticacion-passwordless-imap-stalwart.md) |
| **Playwright Modo Async** | Manejo concurrente de eventos del navegador y sockets de correo sin bloqueos de hilo. | [Ver Arquitectura](#2-c4-nivel-2-diagrama-de-contenedores-container-diagram) |
| **Selectores Centralizados** | Aislamiento de variaciones entre portales de Habilitación y Producción en `DianSelectors`. | [config.py](../src/dian_automation/config.py) |

