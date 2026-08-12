$ErrorActionPreference = 'Stop'
$targets = @(
    (Join-Path $env:USERPROFILE '.vscode\extensions\anthropic.claude-code-2.1.224-win32-x64'),
    (Join-Path $env:USERPROFILE '.vscode\extensions\anthropic.claude-code-2.1.223-win32-x64'),
    (Join-Path $env:USERPROFILE '.vscode\cli\servers\Stable-e4c7e7b1d6d060162f4aa7f8225271b67ce1df75')
)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.vscode')) + '\'
$before = 0L
foreach ($target in $targets) {
    $full = [IO.Path]::GetFullPath($target)
    if (-not $full.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe target: $full" }
    if (Test-Path -LiteralPath $full) {
        $before += [int64]((Get-ChildItem -LiteralPath $full -Force -File -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum)
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Host ('Removed: ' + $full)
    } else {
        Write-Host ('Not found: ' + $full)
    }
}
Write-Host ('Freed approximately ' + [math]::Round($before / 1MB, 1) + ' MB') -ForegroundColor Green
