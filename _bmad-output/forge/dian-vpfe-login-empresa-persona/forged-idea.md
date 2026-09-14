# Forged Idea: Modalidad de Ingreso Empresa y Persona en DIAN VPFE

## Decisiones Clave
- **Modalidades soportadas**:
  - `persona`: Autenticación vía `https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin` con cédula del contribuyente (`#PersonCode`).
  - `empresa`: Autenticación vía `https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin` bajo la modalidad de Representante Legal.
- **Interacción en Login Empresa**:
  - Clic previo en pestaña o botón: `button:has-text("Representante legal")`.
  - Tipeo de cédula de representante: campo `#UserCode:visible`.
  - Tipeo de NIT de empresa: campo `#CompanyCode:visible`.
  - Ambos campos se llenan con cadencia humana (`human_type`).
- **Sanitización de NIT**:
  - El NIT de la empresa se procesa y envía únicamente como dígitos numéricos limpios, sin dígito de verificación ni separadores (puntos o guiones).
- **Tipo de documento**:
  - Se asume por defecto "Cédula de Ciudadanía" para el representante legal, sin pasos adicionales en el dropdown.
- **Centralización de token**:
  - Tanto para persona como para empresa, el correo con el token de acceso llega centralizado al mismo buzón de Stalwart Mail Server (`token@example.com`) monitoreando carpetas `INBOX` y `Junk Mail`/`Spam`.
- **Prevalencia de configuración**:
  - Parámetros CLI (`--tipo`, `--nit-representante`, `--nit-empresa`) tienen máxima prioridad.
  - Variables de entorno en `.env` (`DIAN_LOGIN_TYPE`, `DIAN_REPRESENTATIVE_CODE`, `DIAN_COMPANY_NIT`) actúan como configuración base.
  - Si se suministra `--nit-empresa`, se infiere automáticamente la modalidad `empresa`. Por defecto general, se asume `persona`.
- **Reutilización del núcleo**:
  - Se mantiene íntegro el motor nativo de evasión de Turnstile (Chrome nativo + CDP sin adulteración de prototipos).
  - Se mantiene la configuración directa del rango de fechas "por debajo" con la API de `bootstrap-daterangepicker`.
  - Se mantiene la confirmación `#confirmModal-confirm-button` y la captura de doble evidencia fotográfica.

## Opciones Descartadas
- **Buzones de correo independientes por empresa**: Descartado por confirmación del usuario de que todos los tokens se reciben centralizadamente en `token@example.com`.
- **Selección dinámica de tipos de documento alternativos (Cédula de Extranjería, Pasaporte)**: Descartado por el momento; se estandariza en Cédula de Ciudadanía.
- **Envío de NIT con dígito de verificación**: Descartado; la DIAN en CompanyCode valida exclusivamente el número base sin DV.
