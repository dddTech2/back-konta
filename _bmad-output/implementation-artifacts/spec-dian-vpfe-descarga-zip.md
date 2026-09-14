---
title: 'Descarga automática de listado exportado ZIP en DIAN VPFE con espera hasta 300 segundos'
type: 'feature'
created: '2026-09-13'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Tras solicitar la exportación a Excel en el portal de la DIAN, el archivo no se descarga de forma instantánea sino que se procesa asíncronamente en la tabla `#tableExport`. Se requiere monitorear la fila que coincida con el rango de fechas solicitado hasta que pase a estado "Listo" y descargar el archivo ZIP resultante con un tiempo de espera de hasta 300 segundos.

**Approach:** Implementar sondeo recurrente sobre `#tableExport`, identificando la fila correspondiente al rango solicitado (`Desde {start} Hasta {end}`). Una vez que aparezca el icono de "Listo" (`fa-check`) y el enlace de descarga (`/Document/DownloadExportedZipFile`), disparar la descarga a través del manejador `expect_download` de Playwright, almacenando el archivo ZIP en disco y verificando su tamaño.

</frozen-after-approval>

## Implementation Notes

Ruta oneshot completada. Adaptada dinámicamente para entornos de Habilitación y Producción (`catalogo-vpfe.dian.gov.co` y `catalogo-vpfe-hab.dian.gov.co`). Captura de pantallazos en cada paso del proceso y almacenamiento directo en `./downloads/`.

## Verification

**Commands:**
- `uv run pytest` -- PASSED (13/13 pruebas exitosas en 0.77s)
- `uv run dian-automation --token-url "https://catalogo-vpfe.dian.gov.co/User/AuthToken?pk=00000000|10000002&rk=901008579&token=00000000-0000-0000-0000-000000000000" --mes 2026-08` -- PASSED:
  - Archivo descargado: `downloads/183ff689-751a-4971-82a6-e178e427c1c3.zip` (106,053 bytes)
  - Contenido extraído verificado: `183ff689-751a-4971-82a6-e178e427c1c3.xlsx` (132,563 bytes)
  - Evidencia fotográfica: `screenshot_tabla_reportes.png` y `screenshot_descarga_completada.png`.
