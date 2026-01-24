param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$mainPy = Join-Path $root "main.py"

if (Test-Path $venvPy) {
    & $venvPy $mainPy @Args
    exit $LASTEXITCODE
}

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py $mainPy @Args
    exit $LASTEXITCODE
}

& python $mainPy @Args
exit $LASTEXITCODE
