# Token Saver (agy-tokensaver) — 1-Command Installer & Stats-Preserving Updater (Windows PowerShell)
# Usage: irm https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.ps1 | iex
$ErrorActionPreference = "Stop"

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = @("python") }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $py = @("python3") }
else { throw "Python 3.9+ is required. Install it from https://python.org and re-run." }

$here = if ($PSScriptRoot) { $PSScriptRoot } else { "" }
if ($here -and (Test-Path (Join-Path $here "install.py")) -and (Test-Path (Join-Path $here "plugin.json"))) {
  & $py[0] ($py[1..($py.Length-1)] + @((Join-Path $here "install.py"), "--cli", "--yes"))
  exit $LASTEXITCODE
}

$tmp = Join-Path $env:TEMP "agy-tokensaver-install.py"
Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.py" -OutFile $tmp
try {
  & $py[0] ($py[1..($py.Length-1)] + @($tmp, "--cli", "--yes", "--remote"))
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmp
}
