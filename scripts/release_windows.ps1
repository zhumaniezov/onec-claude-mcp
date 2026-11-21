[CmdletBinding()]
param(
    # Полный путь к 1cv8.exe. По умолчанию берётся самая новая установленная платформа 8.3.
    [string]$PlatformExe,
    # Python 3.11+ для сборки. По умолчанию py -3 или python из PATH.
    [string]$Python
)

# Собирает выпуск в Windows x64 из текущего каталога репозитория:
# CFE из extension/src, автотесты, wheel, EXE, ZIP и контрольные суммы в dist/.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root

function Step([string]$Text) { Write-Host ''; Write-Host ('==> ' + $Text) -ForegroundColor Cyan }
function Run([string]$File, [string[]]$Arguments) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw ('{0} {1} завершился с кодом {2}' -f $File, ($Arguments -join ' '), $LASTEXITCODE) }
}

Step 'Платформа 1С'
if (-not $PlatformExe) {
    $candidates = foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($base) { Get-ChildItem -Path (Join-Path $base '1cv8\8.3.*\bin\1cv8.exe') -ErrorAction SilentlyContinue }
    }
    $PlatformExe = ($candidates | Sort-Object { [version]$_.Directory.Parent.Name } | Select-Object -Last 1).FullName
}
if (-not $PlatformExe -or -not (Test-Path -LiteralPath $PlatformExe)) {
    throw 'Не найден 1cv8.exe. Передайте путь: -PlatformExe "C:\Program Files\1cv8\8.3.27.1644\bin\1cv8.exe"'
}
Write-Host $PlatformExe

Step 'Сборка расширения OneCMCP.cfe'
$built = & (Join-Path $root 'extension\build.ps1') -PlatformExe $PlatformExe | Select-Object -Last 1
Copy-Item -LiteralPath $built.FullName -Destination (Join-Path $root 'extension\OneCMCP.cfe') -Force
Write-Host ('extension\OneCMCP.cfe ' + (Get-FileHash -Algorithm SHA256 (Join-Path $root 'extension\OneCMCP.cfe')).Hash.ToLower())

Step 'Окружение Python'
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $Python = (& py -3 -c 'import sys; print(sys.executable)').Trim() }
    else { $Python = (Get-Command python).Source }
}
$venv = Join-Path $root '.venv-release'
if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
Run $Python @('-m', 'venv', $venv)
$py = Join-Path $venv 'Scripts\python.exe'
Run $py @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
Run $py @('-m', 'pip', 'install', '--quiet', '-e', '.', 'build', 'pyinstaller==6.22.3')

Step 'Автотесты'
Run $py @('-m', 'unittest', 'discover', '-s', 'tests')

Step 'Wheel, EXE и архив'
if (Test-Path -LiteralPath (Join-Path $root 'dist')) { Remove-Item -LiteralPath (Join-Path $root 'dist') -Recurse -Force }
Run $py @('-m', 'build', '--wheel')
Run $py @('scripts/build_windows.py')
Run $py @('scripts/package_windows.py')
Run (Join-Path $root 'dist\windows\onec-claude-mcp.exe') @('--version')

Step 'Контрольные суммы'
Copy-Item -LiteralPath (Join-Path $root 'extension\OneCMCP.cfe') -Destination (Join-Path $root 'dist\OneCMCP.cfe') -Force
$assets = Get-ChildItem -LiteralPath (Join-Path $root 'dist') -File | Where-Object { $_.Extension -in '.whl', '.zip', '.cfe' }
$sums = $assets | ForEach-Object { '{0}  {1}' -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower(), $_.Name }
$sums | Set-Content -Encoding ascii -LiteralPath (Join-Path $root 'dist\SHA256SUMS.txt')
$sums | Write-Host

Write-Host ''
Write-Host ('Готово. Файлы выпуска: ' + (Join-Path $root 'dist')) -ForegroundColor Green
