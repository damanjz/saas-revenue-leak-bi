# Crop a Power BI window capture to the report canvas. The canvas is found by its paper colour,
# so no pixel coordinates are hard-coded; the 16:9 page sets the height from the width.
param([Parameter(Mandatory)][string]$In, [Parameter(Mandatory)][string]$Out)
Add-Type -AssemblyName System.Drawing
$bmp = [System.Drawing.Bitmap]::FromFile($In)
function IsPaper($c) { [Math]::Abs($c.R - 0xfb) -le 2 -and [Math]::Abs($c.G - 0xfa) -le 2 -and [Math]::Abs($c.B - 0xf7) -le 2 }
$midY = [int]($bmp.Height * 0.62)
$left = -1
for ($x = 60; $x -lt $bmp.Width / 2; $x++) { if (IsPaper $bmp.GetPixel($x, $midY)) { $left = $x; break } }
if ($left -lt 0) { "canvas not found"; exit 1 }
$probe = $left + 2
$top = $midY; while ($top -gt 0 -and (IsPaper $bmp.GetPixel($probe, $top - 1))) { $top-- }
$bottom = $midY; while ($bottom -lt $bmp.Height - 1 -and (IsPaper $bmp.GetPixel($probe, $bottom + 1))) { $bottom++ }
# the top margin row is paper across the whole canvas: it gives the right edge
$right = $left; while ($right -lt $bmp.Width - 1 -and (IsPaper $bmp.GetPixel($right + 1, $top + 3))) { $right++ }
$width = $right - $left + 1
$height = [int][Math]::Round($width * 720 / 1280)
$crop = $bmp.Clone((New-Object System.Drawing.Rectangle $left, $top, $width, $height), $bmp.PixelFormat)
$bmp.Dispose()
$crop.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
"saved $Out ($width x $height)"
exit 0
