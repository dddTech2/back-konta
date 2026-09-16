# Auditoría de Variables de Entorno

> Documento generado automáticamente por `codebase-documenter`.

Este catálogo consolida todas las variables de entorno consumidas en el código fuente, cruzadas con los archivos `.env.example` y `.env`.

## Resumen
- **Variables detectadas en código:** 14
- **Variables definidas en .env.example:** 14

---

## Tabla de Variables

| Variable | Descripción / Rol | Valor por Defecto / Ejemplo | Archivos donde se consume | Estado |
| :--- | :--- | :--- | :--- | :--- |
| `DIAN_COMPANY_NIT` |  | —, `901008579` (ejemplo) | `src/dian_automation/config.py:35` | ✅ Documentada |
| `DIAN_COMPANY_URL` |  | `https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin`, `https://catalogo-vpfe-hab.dian.gov.co/User/CompanyLogin` (ejemplo) | `src/dian_automation/config.py:32` | ✅ Documentada |
| `DIAN_LOGIN_TYPE` | Modalidad de ingreso por defecto: 'persona' o 'empresa' | `persona`, `persona` (ejemplo) | `src/dian_automation/config.py:30` | ✅ Documentada |
| `DIAN_PERSON_CODE` | Modo Persona Natural: Cédula de Ciudadanía | `1000000001`, `1000000001` (ejemplo) | `src/dian_automation/config.py:33` | ✅ Documentada |
| `DIAN_REPRESENTATIVE_CODE` | Modo Empresa: Cédula del Representante Legal y NIT de la Empresa (sin DV) | —, `10000002` (ejemplo) | `src/dian_automation/config.py:34` | ✅ Documentada |
| `DIAN_URL` | Habilitación: | `https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin`, `https://catalogo-vpfe-hab.dian.gov.co/User/PersonLogin` (ejemplo) | `src/dian_automation/config.py:31` | ✅ Documentada |
| `DOWNLOAD_DIR` | Carpeta de destino de los archivos ZIP descargados | `./downloads`, `./downloads` (ejemplo) | `src/dian_automation/config.py:45` | ✅ Documentada |
| `EMAIL_TIMEOUT_SECONDS` | Tiempo máximo de espera para la llegada del correo con el token (segundos) | `60`, `60` (ejemplo) | `src/dian_automation/config.py:43` | ✅ Documentada |
| `EXPORT_DOWNLOAD_TIMEOUT_SECONDS` | Tiempo máximo de espera para que la DIAN procese y ponga en 'Listo' el ZIP (segundos) | `300`, `300` (ejemplo) | `src/dian_automation/config.py:44` | ✅ Documentada |
| `HEADLESS` | Ejecución visible o segundo plano (False recomendado para evitar bloqueos) | `False`, `False` (ejemplo) | `src/dian_automation/config.py:42` | ✅ Documentada |
| `STALWART_IMAP_HOST` | Servidor de Correo Stalwart (Recepción automática del token mágico) | `mail.example.com`, `mail.example.com` (ejemplo) | `src/dian_automation/config.py:37` | ✅ Documentada |
| `STALWART_IMAP_PORT` |  | `993`, `993` (ejemplo) | `src/dian_automation/config.py:38` | ✅ Documentada |
| `STALWART_PASSWORD` |  | —, `tu_contrasena_aqui` (ejemplo) | `src/dian_automation/config.py:40` | ✅ Documentada |
| `STALWART_USER` |  | `token@example.com`, `token@example.com` (ejemplo) | `src/dian_automation/config.py:39` | ✅ Documentada |

---

## Recomendaciones de Seguridad
1. Nunca agregues claves de producción ni contraseñas a repositorios públicos.
2. Mantén siempre actualizado el archivo `.env.example` cuando agregues nuevas variables al código.
3. Emplea gestores de secretos (Vault, Doppler, AWS Secrets Manager) para despliegues de producción.
