param(
    [Parameter(Mandatory=$true)]
    [string]$BaseDir,
    [Parameter(Mandatory=$true)]
    [string]$ScriptPath,
    [string]$Prefix = "takeout-YYYYMMDDThhmmssZ-part-",
    [int]$Start = 1,
    [int]$End = 1
)

if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "Python script not found: $ScriptPath" }
if (-not (Test-Path -LiteralPath $BaseDir)) { throw "Base folder not found: $BaseDir" }

for ($i = $Start; $i -le $End; $i++) {
    $suffix = "{0:D3}" -f $i
    $takeoutFolder = Join-Path $BaseDir ($Prefix + $suffix)
    if (-not (Test-Path -LiteralPath $takeoutFolder)) {
        Write-Host "[SKIP] Missing folder: $takeoutFolder"
        continue
    }
    Write-Host "[RUN ] $takeoutFolder"
    python3 $ScriptPath $takeoutFolder
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] Exit code $LASTEXITCODE on $takeoutFolder"
        break
    }
    Write-Host "[ OK ] $takeoutFolder"
}
Write-Host "Done."
