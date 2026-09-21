<#
.SYNOPSIS
    Registra (o quita) la tarea programada "Kontable Worker" que mantiene corriendo kontable-worker-remote.

.DESCRIPTION
    La tarea arranca al iniciar sesión el usuario actual y se reinicia sola si el worker termina con
    error. Corre en la sesión interactiva porque Chrome debe abrirse con ventana (HEADLESS=False es lo
    recomendado contra Turnstile); por eso no es un servicio de Windows. En un equipo dedicado, deja el
    inicio de sesión automático activado para que el worker arranque tras un reinicio.

.PARAMETER Uninstall
    Elimina la tarea (opción inversa a la instalación).

.PARAMETER StartNow
    Al instalar, además la inicia de inmediato sin esperar al próximo inicio de sesión.

.PARAMETER ProjectDir
    Carpeta de ProyectoDianBack (donde están pyproject.toml y el .env). Por defecto, dos niveles arriba
    de este script.

.PARAMETER TaskName
    Nombre de la tarea. Por defecto "Kontable Worker".

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ops\install_worker_task.ps1 -StartNow

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\ops\install_worker_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$StartNow,
    [string]$ProjectDir = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$TaskName = "Kontable Worker"
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Tarea '$TaskName' eliminada."
    } else {
        Write-Host "La tarea '$TaskName' no existe; no hay nada que quitar."
    }
    return
}

$projectFile = Join-Path $ProjectDir "pyproject.toml"
if (-not (Test-Path $projectFile)) {
    throw "No se encontró $projectFile. Indica la carpeta de ProyectoDianBack con -ProjectDir."
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Warning "No hay .env en ${ProjectDir}: el worker necesita KONTABLE_API_URL e INTERNAL_WORKER_TOKEN (ver docs/DEPLOYMENT.md)."
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    throw "No se encontró 'uv' en el PATH. Instálalo (https://docs.astral.sh/uv/) y vuelve a ejecutar."
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $ProjectDir "worker.log"
# cmd /s /c "..." quita solo las comillas externas y deja las internas: sirve con rutas con espacios.
$arguments = "/d /s /c `"`"$($uv.Source)`" run kontable-worker-remote >> `"$logFile`" 2>&1`""

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arguments -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Worker remoto de Konta: descarga los reportes de la DIAN y los sube a la API." `
    -Force | Out-Null

Write-Host "Tarea '$TaskName' registrada para ${user}: arranca al iniciar sesión y se reinicia si falla."
Write-Host "El registro del worker queda en $logFile."

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Tarea iniciada."
}
