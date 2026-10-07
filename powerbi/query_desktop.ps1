# Run a DAX query against the model in the Power BI window opened by open_pbip.ps1 -Keep; print rows as JSON.
param([Parameter(Mandatory)][string]$Dax)
$root = Split-Path $PSScriptRoot -Parent
$port = (Get-Content "$root\.captures\pbi-port.txt").Trim()
Add-Type -Path "C:\Program Files\Microsoft Power BI Desktop\bin\Microsoft.PowerBI.AdomdClient.dll"
$conn = New-Object Microsoft.AnalysisServices.AdomdClient.AdomdConnection "Data Source=localhost:$port"
$conn.Open()
$cmd = $conn.CreateCommand(); $cmd.CommandText = $Dax
$r = $cmd.ExecuteReader()
$rows = @()
while ($r.Read()) {
    $o = [ordered]@{}
    for ($i = 0; $i -lt $r.FieldCount; $i++) { $o[$r.GetName($i)] = $r.GetValue($i) }
    $rows += [pscustomobject]$o
}
$conn.Close()
$rows | ConvertTo-Json -Depth 3 -Compress
