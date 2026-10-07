# Print docs/<name>.html to PDF with headless Edge (no headers or footers). With -Review, also render every
# page to .captures/pdf/<name>-pN.png with Windows' own PDF renderer, so the layout can be checked page by page.
param([string]$Name = "case-study", [switch]$Review)
$docs = $PSScriptRoot; $root = Split-Path $docs -Parent
$src = Join-Path $docs "$Name.html"; $pdf = Join-Path $docs "$Name.pdf"
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
$profile = Join-Path $root ".captures\edge-profile"
Start-Process $edge -ArgumentList "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--user-data-dir=`"$profile`"",
    "--print-to-pdf=`"$pdf`"", "`"file:///$($src.Replace('\', '/'))`"" -Wait
Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue
if (-not $Review) { "${pdf}: $([math]::Round((Get-Item $pdf).Length / 1KB)) KB"; exit 0 }

Add-Type -AssemblyName System.Drawing, System.Runtime.WindowsRuntime
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 }
$asOp = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asAct = ($ext | Where-Object { $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($op, $type) { $t = $asOp.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(); $t.Result }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pdf)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$out = Join-Path $root ".captures\pdf"; New-Item -ItemType Directory -Force $out | Out-Null
Get-ChildItem $out -Filter "$Name-p*.png" | Remove-Item
for ($i = 0; $i -lt $doc.PageCount; $i++) {
    $page = $doc.GetPage($i); $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
    $opt = New-Object Windows.Data.Pdf.PdfPageRenderOptions; $opt.DestinationWidth = 900
    $asAct.Invoke($null, @($page.RenderToStreamAsync($stream, $opt))).Wait()
    $bmp = [System.Drawing.Bitmap]::FromStream([System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream))
    $bmp.Save((Join-Path $out "$Name-p$($i + 1).png"), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose(); $page.Dispose()
}
"${pdf}: $($doc.PageCount) pages, $([math]::Round((Get-Item $pdf).Length / 1KB)) KB"
