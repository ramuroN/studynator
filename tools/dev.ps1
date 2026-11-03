# tools/dev.ps1
# Startet Studynator lokal unter Windows, ohne Docker. Venv und Daten liegen unter
# %LOCALAPPDATA%\study, nicht im synchronisierten Ordner. Aus dem Hauptordner:
#
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\dev.ps1
#
# Ohne Schalter erzeugt der Anbieter «fake» Vorschläge aus der ersten Zeile jeder
# Seite (kostet nichts). Mit -Anthropic geht es an die Claude-API; dafür muss
# ANTHROPIC_API_KEY in dieser Sitzung gesetzt sein. Pro Datenordner läuft nur ein
# Server; für einen zweiten (z. B. die Vorschau) -DataDir angeben.

param(
  [int]$Port = 8790,
  [switch]$Anthropic,
  [string]$Model = 'claude-opus-5-5',
  [string]$DataDir = '',
  # Pakete nicht prüfen (schneller; für die Vorschau neben einem laufenden Server)
  [switch]$SkipInstall,
  # Konto ohne Anmeldung; für eine wiederhergestellte Kopie das Konto des Besitzers
  [string]$User = 'dev',
  # Ohne Worker: keine Jobs, keine Tageskopie (z. B. für einen Restore-Test)
  [switch]$NoWorker
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$base = Join-Path $env:LOCALAPPDATA 'study'
$venv = Join-Path $base 'venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $python)) {
  Write-Host 'Lege die venv an ...'
  py -3.13 -m venv $venv
  if ($LASTEXITCODE -ne 0) { throw 'venv fehlgeschlagen' }
}
if (-not $SkipInstall) {
  & $python -m pip install -q --disable-pip-version-check -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw 'pip install fehlgeschlagen' }
}

if ($Anthropic -and -not $env:ANTHROPIC_API_KEY) {
  throw 'Für -Anthropic zuerst ANTHROPIC_API_KEY in dieser Sitzung setzen.'
}

$env:STUDY_DATA_DIR = if ($DataDir) { $DataDir } else { Join-Path $base 'data' }
$env:STUDY_DEV_USER = $User
$env:STUDY_WORKER = if ($NoWorker) { '0' } else { '1' }
$env:STUDY_AI_PROVIDER = if ($Anthropic) { 'anthropic' } else { 'fake' }
$env:STUDY_MODEL = $Model
Remove-Item Env:PROXY_SECRET -ErrorAction SilentlyContinue

Write-Host "Studynator auf http://127.0.0.1:$Port (Anbieter $env:STUDY_AI_PROVIDER, Daten $env:STUDY_DATA_DIR)"
& $python -m uvicorn study.app:app --host 127.0.0.1 --port $Port
