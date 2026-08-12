$ErrorActionPreference = 'SilentlyContinue'
$processNames = @('chrome','msedge','quark','steam','baidunetdisk','CloudMusic','FlashCenter','DingTalk','DingTalk_91','qianwen','wemeet','WeMeeting')
$running = Get-Process | Where-Object { $processNames -contains $_.ProcessName } | Select-Object -ExpandProperty ProcessName -Unique
if ($running) {
    Write-Host ('Close these applications, then run this script again: ' + ($running -join ', ')) -ForegroundColor Yellow
    exit 2
}

$relativeTargets = @(
    'AppData\Local\Google\Chrome\User Data\Default\Cache',
    'AppData\Local\Google\Chrome\User Data\component_crx_cache',
    'AppData\Local\Quark\User Data\Default\Cache',
    'AppData\Local\Quark\User Data\Default\Code Cache',
    'AppData\Roaming\360huabao\360yasuo\user_data\chromeshell\Default\Cache',
    'AppData\Local\Steam\htmlcache\Default\Cache',
    'AppData\Local\Steam\htmlcache\Default\Code Cache',
    'AppData\Local\Steam\htmlcache\Default\component_crx_cache',
    'AppData\Roaming\baidunetdisk\Cache',
    'AppData\Roaming\baidunetdisk\Code Cache',
    'AppData\Local\Netease\CloudMusic\webapp91x64\Cache',
    'AppData\Local\Netease\CloudMusic\webapp91x64\Code Cache',
    'AppData\Local\Flash_Center\cache',
    'AppData\Roaming\DingTalk\Cache',
    'AppData\Local\DingTalk_91\Cache',
    'AppData\Local\DingTalk_91\Code Cache',
    'AppData\Local\Qianwen\User Data\Default\Cache',
    'AppData\Roaming\Tencent\WeMeet\Global\Logs',
    'AppData\Roaming\WeMeeting\logs',
    'AppData\Local\Microsoft\Edge\User Data\Default\Cache',
    'AppData\Local\Microsoft\Edge\User Data\Default\Code Cache',
    'AppData\Local\Microsoft\Edge\User Data\component_crx_cache',
    'AppData\Local\Microsoft\Edge\User Data\Crashpad'
)

$allowedRoot = [IO.Path]::GetFullPath($env:USERPROFILE) + '\'
$targets = $relativeTargets | ForEach-Object { Join-Path $env:USERPROFILE $_ }
$before = 0L
$removed = 0
foreach ($target in $targets) {
    $full = [IO.Path]::GetFullPath($target)
    if (-not $full.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe target: $full" }
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $size = (Get-ChildItem -LiteralPath $full -Force -File -Recurse | Measure-Object Length -Sum).Sum
    $before += [int64]$size
    Remove-Item -LiteralPath $full -Recurse -Force
    if (-not (Test-Path -LiteralPath $full)) { $removed++ }
}

$after = 0L
foreach ($target in $targets) {
    if (Test-Path -LiteralPath $target) {
        $after += [int64]((Get-ChildItem -LiteralPath $target -Force -File -Recurse | Measure-Object Length -Sum).Sum)
    }
}
Write-Host ('Removed directories: ' + $removed + '/' + $targets.Count)
Write-Host ('Freed: ' + [math]::Round(($before - $after) / 1MB, 1) + ' MB') -ForegroundColor Green
