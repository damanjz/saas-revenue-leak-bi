# Measure rendered text widths in pixels (96 dpi) with the report's real fonts.
# Input: JSON file of [{ "id", "text", "font", "pt", "bold" }]. Output: JSON [{ "id", "px" }].
param([Parameter(Mandatory)][string]$InFile)
Add-Type -AssemblyName System.Drawing
$items = Get-Content $InFile -Raw -Encoding UTF8 | ConvertFrom-Json
$bmp = New-Object System.Drawing.Bitmap 1, 1
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.PageUnit = [System.Drawing.GraphicsUnit]::Pixel
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
$fmt = [System.Drawing.StringFormat]::GenericTypographic
$fmt.FormatFlags = $fmt.FormatFlags -bor [System.Drawing.StringFormatFlags]::MeasureTrailingSpaces
$cache = @{}
$out = foreach ($it in $items) {
    $key = "$($it.font)|$($it.pt)|$($it.bold)"
    if (-not $cache.ContainsKey($key)) {
        $style = if ($it.bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
        $cache[$key] = New-Object System.Drawing.Font ($it.font, [single]$it.pt, $style, [System.Drawing.GraphicsUnit]::Point)
    }
    $size = $g.MeasureString([string]$it.text, $cache[$key], 100000, $fmt)
    [pscustomobject]@{ id = $it.id; px = [math]::Round($size.Width, 1) }
}
@($out) | ConvertTo-Json -Compress
