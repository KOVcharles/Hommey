$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskEnvFile = Join-Path $taskRoot '.env.engineering'
if (!(Test-Path -LiteralPath $taskEnvFile)) { throw 'Run scripts/prepare_engineering.py first.' }
foreach ($taskLine in Get-Content -LiteralPath $taskEnvFile -Encoding utf8) {
    if ($taskLine -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"').Trim("'"), 'Process')
    }
}
if (!$env:JAVA_HOME) {
    $taskCompiler = Get-Command javac -ErrorAction Stop
    $env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $taskCompiler.Source)
}
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
Push-Location (Join-Path $taskRoot 'backend')
try { & './mvnw.cmd' spring-boot:run; if ($LASTEXITCODE -ne 0) { throw 'Backend exited with an error.' } }
finally { Pop-Location }
