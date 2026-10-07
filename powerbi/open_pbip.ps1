# Open the project in its own Power BI Desktop window, load the data, capture that window, then close only that window.
# Other Power BI windows are never touched. -Keep leaves the window open (for DAX queries with query_desktop.ps1).
param([string]$Page = "revenue", [switch]$Keep, [switch]$NoBuild, [string]$Out, [string]$Project, [string]$Python = "python")
$root = Split-Path $PSScriptRoot -Parent
$caps = "$root\.captures"
New-Item -ItemType Directory -Force $caps | Out-Null
if (-not $Out) { $Out = "$caps\pbi-$Page.png" }
if (-not $Project) { $Project = "$root\powerbi\RevenueLeak.pbip" }
$projectDir = Split-Path $Project -Parent
if (-not $NoBuild) {
    & "$root\.venv\Scripts\python.exe" "$root\powerbi\build_pbip.py" | Out-Null
    & "$PSScriptRoot\validate_tmdl.ps1"; if ($LASTEXITCODE) { exit 1 }
}
$pj = "$projectDir\RevenueLeak.Report\definition\pages\pages.json"
$meta = Get-Content $pj -Raw | ConvertFrom-Json; $meta.activePageName = $Page
[IO.File]::WriteAllText($pj, ($meta | ConvertTo-Json -Depth 5))

function Close-Own($proc) {
    # close only the window this script started (and its engine), then wait until it has really gone
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    $proc.WaitForExit(30000) | Out-Null
    Start-Sleep -Seconds 3
}

$before = @(Get-Process msmdsrv -ErrorAction SilentlyContinue | ForEach-Object Id)
$started = Get-Date
$p = Start-Process "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe" -ArgumentList "`"$Project`"" -PassThru
$deadline = $started.AddMinutes(5)
try {
    do { Start-Sleep -Seconds 3; $p.Refresh() } until ($p.MainWindowTitle -like "RevenueLeak*" -or $p.HasExited -or (Get-Date) -gt $deadline)
    if ($p.HasExited -or $p.MainWindowTitle -notlike "RevenueLeak*") { throw "Power BI did not open the project (title: $($p.MainWindowTitle))" }

    # this window's own analysis engine: the msmdsrv started after we launched
    do {
        Start-Sleep -Seconds 2
        $engine = Get-Process msmdsrv -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | Select-Object -First 1
    } until ($engine -or (Get-Date) -gt $deadline)
    $portFile = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\Power BI Desktop\AnalysisServicesWorkspaces" -Recurse -Filter msmdsrv.port.txt |
        Where-Object LastWriteTime -gt $started | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $engine -or -not $portFile) { throw "no analysis engine for our window" }
    $port = (Get-Content $portFile.FullName -Encoding Unicode).Trim()
    Set-Content "$caps\pbi-port.txt" $port
    Add-Type -Path "C:\Program Files\Microsoft Power BI Desktop\bin\Microsoft.PowerBI.AdomdClient.dll"
    $c = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection ("Data Source=localhost:$port")
    $c.Open(); $cmd = $c.CreateCommand()
    $cmd.CommandText = '{"refresh":{"type":"full","objects":[{"database":"' + $c.Database + '"}]}}'
    $cmd.ExecuteNonQuery() | Out-Null; $c.Close()
    "opened and refreshed (port $port, pid $($p.Id))"
    Start-Sleep -Seconds 12

    Add-Type -AssemblyName System.Drawing
    Add-Type @"
using System; using System.Runtime.InteropServices;
public class PbiCap {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
}
"@
    # maximise our window so the canvas renders at full size; the main handle can briefly be a helper window
    $r = New-Object PbiCap+RECT
    for ($try = 0; $try -lt 12; $try++) {
        $p.Refresh()
        [PbiCap]::ShowWindow($p.MainWindowHandle, 3) | Out-Null
        Start-Sleep -Seconds 5
        [PbiCap]::GetWindowRect($p.MainWindowHandle, [ref]$r) | Out-Null
        if (($r.R - $r.L) -ge 800) { break }
    }
    Start-Sleep -Seconds 6
    if (($r.R - $r.L) -lt 800) { throw "our window is not showing the report (width $($r.R - $r.L))" }
    $bmp = New-Object System.Drawing.Bitmap ($r.R - $r.L), ($r.B - $r.T)
    $g = [System.Drawing.Graphics]::FromImage($bmp); $dc = $g.GetHdc()
    [PbiCap]::PrintWindow($p.MainWindowHandle, $dc, 2) | Out-Null; $g.ReleaseHdc($dc)
    $bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
    "saved $Out ($($bmp.Width) x $($bmp.Height))"
} catch {
    # a failed launch never leaves its window (or an error dialog) behind
    Close-Own $p
    "failed: $($_.Exception.Message); closed our window (pid $($p.Id))"
    exit 1
}

if (-not $Keep) {
    Close-Own $p
    "closed our window (pid $($p.Id))"
}
exit 0
