param(
    [Parameter(Mandatory=$true)]
    [string]$SourceRoot,
    [Parameter(Mandatory=$true)]
    [string]$DestRoot,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Get-TakeoutLabel([string]$fullPath) {
    if ($fullPath -match 'takeout-\d{8}T\d+Z-\d-(\d{3})') {
        $n = [int]$matches[1]
        return "Takeout $n"
    }
    if ($fullPath -match 'Takeout\s+(\d+)') {
        return "Takeout $($matches[1])"
    }
    return "Takeout_Unknown"
}

function Get-UniqueDest([string]$baseDir, [string]$name) {
    $candidate = Join-Path $baseDir $name
    if (-not (Test-Path -LiteralPath $candidate)) { return $candidate }
    $i = 2
    while ($true) {
        $cand = Join-Path $baseDir ("{0}__{1}" -f $name, $i)
        if (-not (Test-Path -LiteralPath $cand)) { return $cand }
        $i++
    }
}

if (-not (Test-Path -LiteralPath $SourceRoot)) { throw "SourceRoot not found: $SourceRoot" }
if (-not (Test-Path -LiteralPath $DestRoot)) { New-Item -ItemType Directory -Path $DestRoot | Out-Null }

$folders = Get-ChildItem -LiteralPath $SourceRoot -Directory -Recurse | Where-Object { $_.Name -eq "_UPLOAD_READY" }
Write-Host "Found _UPLOAD_READY folders: $($folders.Count)"

foreach ($f in $folders) {
    $label = Get-TakeoutLabel $f.FullName
    $dest = Get-UniqueDest $DestRoot $label
    if ($DryRun) {
        Write-Host "[DRY_RUN] $($f.FullName) -> $dest"
        continue
    }
    Move-Item -LiteralPath $f.FullName -Destination $dest
    Write-Host "[OK] $($f.FullName) -> $dest"
}
