# Parse the TMDL model with Power BI Desktop's own serializer, without opening Desktop.
param([string]$Folder = "$PSScriptRoot\RevenueLeak.SemanticModel\definition")
$bin = "C:\Program Files\Microsoft Power BI Desktop\bin"
# Resolve Power BI's assemblies from its bin folder (a compiled handler: a script-block handler can recurse)
Add-Type -TypeDefinition @"
using System; using System.IO; using System.Reflection;
public static class PbiResolver {
    public static string Bin;
    public static void Hook() { AppDomain.CurrentDomain.AssemblyResolve += Resolve; }
    static Assembly Resolve(object s, ResolveEventArgs e) {
        string f = Path.Combine(Bin, new AssemblyName(e.Name).Name + ".dll");
        return File.Exists(f) ? Assembly.LoadFrom(f) : null;
    }
}
"@
[PbiResolver]::Bin = $bin
[PbiResolver]::Hook()
$asm = [Reflection.Assembly]::LoadFrom("$bin\Microsoft.PowerBI.Tabular.dll")
$ser = $asm.GetType("Microsoft.AnalysisServices.Tabular.TmdlSerializer")
try {
    $db = $ser.GetMethod("DeserializeDatabaseFromFolder", [type[]]@([string])).Invoke($null, @($Folder))
    $m = $db.Model
    "TMDL OK: $($m.Tables.Count) tables, $(($m.Tables | % { $_.Measures.Count } | Measure -Sum).Sum) measures, $($m.Relationships.Count) relationships"
} catch {
    $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
    "TMDL FAIL: $($e.Message)"; exit 1
}
