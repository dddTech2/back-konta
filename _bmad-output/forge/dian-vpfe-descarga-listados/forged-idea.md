# Forged Idea: Automatización DIAN VPFE y Descarga de Listados

## Decisiones Clave
- **Autenticación sin UI de correo**: Consulta de token mediante conexión directa IMAP/API a Stalwart Mail Server (`token@example.com`), eliminando la fragilidad de automatizar webmail en navegador.
- **Mapeo y extracción de token**: Filtrado de correos posteriores al clic en "Entrar" y extracción de la URL desde la etiqueta `<a>` con la imagen `ingreso`.
- **Continuidad de sesión**: Navegación directa `page.goto(tokenUrl)` en el mismo `BrowserContext` de Playwright para persistir cookies y sesión.
- **Manejo de rango de fechas**: Lectura del valor actual de `#export-range` y desplazamiento iterativo día a día mediante `#action-left` o `#action-right` hasta alcanzar la fecha objetivo.
- **Alcance Milestone 1**: Desde el login inicial hasta la confirmación de encolamiento (clic en `Exportar Excel` -> `SI` -> cerrar modal `×`). La descarga efectiva desde la tabla inferior queda delimitada para Milestone 2 tras inspeccionar la tabla en vivo.

## Opciones Descartadas
- **Automatizar Webmail en pestaña de navegador**: Descartado por alta tasa de fallos, sobrecarga de recursos y fragilidad ante cambios de DOM.
- **Descarga directa en Milestone 1**: Descartado temporalmente debido a que la DIAN encola la petición en una tabla asíncrona cuyo selector de descarga se identificará una vez encolado el primer reporte.
