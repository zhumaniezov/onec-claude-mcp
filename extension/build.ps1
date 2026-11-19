[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PlatformExe,
    [string]$BuildRoot = [System.IO.Path]::GetTempPath()
)

$ErrorActionPreference = 'Stop'
$platform = (Resolve-Path -LiteralPath $PlatformExe).Path
$source = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'src')).Path
$stage = Join-Path ([System.IO.Path]::GetFullPath($BuildRoot)) ('onec-mcp-' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $stage)
$database = Join-Path $stage 'base'
$artifact = Join-Path $stage 'OneCMCP.cfe'

function Invoke-BatchStep {
    param([string]$Name, [string]$Arguments)
    $log = Join-Path $stage ($Name + '.log')
    $batchArguments = $Arguments + (' /Lru /DisableStartupDialogs /DisableStartupMessages /Out "{0}"' -f $log)
    $process = Start-Process -FilePath $platform -ArgumentList $batchArguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        if (Test-Path -LiteralPath $log) { Get-Content -Encoding UTF8 -LiteralPath $log | Write-Host }
        throw ('{0} failed with code {1}. Working directory: {2}' -f $Name, $process.ExitCode, $stage)
    }
}

Invoke-BatchStep 'create' ('CREATEINFOBASE File="{0}"' -f $database)
$designer = 'DESIGNER /F "{0}" ' -f $database
Invoke-BatchStep 'load' ($designer + ('/LoadConfigFromFiles "{0}" -Extension OneCMCP' -f $source))
Invoke-BatchStep 'apply' ($designer + '/UpdateDBCfg -Extension OneCMCP')
Invoke-BatchStep 'check' ($designer + '/CheckConfig -Extension OneCMCP -CheckModules -Server -ExternalConnection')
Invoke-BatchStep 'dump' ($designer + ('/DumpCfg "{0}" -Extension OneCMCP' -f $artifact))
if (!(Test-Path -LiteralPath $artifact -PathType Leaf)) { throw '1C did not produce a CFE file.' }
Get-Item -LiteralPath $artifact
