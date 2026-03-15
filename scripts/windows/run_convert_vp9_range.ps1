$BaseDir = "D:\Work\TakeoutFiles"
$ScriptPath = "D:\Work\convert_vp9_to_h264.py"
if (-not (Test-Path -LiteralPath $ScriptPath)) { $ScriptPath = "D:\Work\convert_vp9_to_h264" }
$Prefix = "takeout-20260221T191720Z-3-"
$Start = 28
$End = 36

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
