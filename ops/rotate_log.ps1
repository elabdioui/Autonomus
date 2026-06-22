param(
    [Parameter(Mandatory=$true)][string]$Path,
    [long]$MaxBytes = 10485760,
    [int]$Backups = 4
)

if (-not (Test-Path -LiteralPath $Path)) { exit 0 }
if ((Get-Item -LiteralPath $Path).Length -le $MaxBytes) { exit 0 }

$oldest = "$Path.$Backups"
if (Test-Path -LiteralPath $oldest) { Remove-Item -LiteralPath $oldest -Force }
for ($i = $Backups - 1; $i -ge 1; $i--) {
    $source = "$Path.$i"
    if (Test-Path -LiteralPath $source) {
        Move-Item -LiteralPath $source -Destination "$Path.$($i + 1)" -Force
    }
}
Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
