# Report images for the docs: open each page in its own Power BI window (never one the user has open),
# crop the capture to the report canvas and save it to docs\img. Raw window captures are deleted afterwards.
$root = Split-Path $PSScriptRoot -Parent
$img = "$root\docs\img"
New-Item -ItemType Directory -Force $img | Out-Null
$pages = @(@("revenue", "01-revenue"), @("cohorts", "02-cohorts"), @("accounts", "03-accounts"), @("account", "04-account"))
foreach ($p in $pages) {
    $raw = "$root\.captures\doc-$($p[1]).png"
    # Power BI sometimes fails to open a project on the first try; each attempt cleans up after itself
    for ($try = 1; $try -le 3; $try++) {
        & "$root\powerbi\open_pbip.ps1" -Page $p[0] -NoBuild -Out $raw | Out-Null
        if ($LASTEXITCODE -eq 0) { & "$root\powerbi\crop_canvas.ps1" -In $raw -Out "$img\$($p[1]).png" }
        if ($LASTEXITCODE -eq 0) { break }
        "$($p[1]): attempt $try failed"
    }
}
Get-ChildItem "$root\.captures" -Filter "doc-*.png" | Remove-Item
