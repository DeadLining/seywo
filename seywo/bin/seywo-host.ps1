# Windows PowerShell 5.1. Never resolve Python or PowerShell through PATH.
$ErrorActionPreference = 'Stop'

function Find-BundleRoots {
    if ($env:SEYWO_CODEX_RUNTIME) { return $env:SEYWO_CODEX_RUNTIME }
    $relative = 'codex-runtimes\codex-primary-runtime'
    $default = [IO.Path]::Combine($env:USERPROFILE, '.cache', $relative)
    $marker = [IO.FileInfo]::new([IO.Path]::Combine($default, 'selected-framework'))
    if ($marker.Exists -and $marker.Length -le 32768) {
        # Codex writes the selected MSIX runtime root as UTF-8, not shell code.
        $selected = [IO.File]::ReadAllText($marker.FullName).TrimEnd("`r", "`n")
        if ($selected -match '^[A-Za-z]:[\\/]' -and $selected -notmatch '[\x00-\x1f"]') { $selected }
    }
    if ($env:XDG_CACHE_HOME) { [IO.Path]::Combine($env:XDG_CACHE_HOME, $relative) }
    $default
    if ($env:LOCALAPPDATA) { [IO.Path]::Combine($env:LOCALAPPDATA, $relative) }
}

function Find-Interpreter {
    foreach ($root in (Find-BundleRoots)) {
        if ($root -notmatch '^[A-Za-z]:[\\/]' -or -not [IO.File]::Exists([IO.Path]::Combine($root, 'runtime.json'))) { continue }
        foreach ($name in 'python.exe', 'python\python.exe', 'bin\python.exe') {
            $exe = [IO.Path]::Combine($root, 'dependencies\python', $name)
            if ([IO.File]::Exists($exe)) { return @{ Root = $root; Exe = $exe } }
        }
    }
}

function Quote-NativeArgument([string]$value) {
    # ProcessStartInfo on .NET Framework takes a command line, not ArgumentList.
    '"' + (($value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

try {
    $bundle = Find-Interpreter
    if (-not $bundle) {
        [Console]::Error.WriteLine('Seywo [runtime_missing]: Codex workspace Python is unavailable.')
        exit 1
    }
    $nativeArgs = @('-IBS', '-Xutf8', [IO.Path]::Combine($PSScriptRoot, 'seywo-host.py'), '--runtime-root', $bundle.Root) + $args
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $bundle.Exe
    $start.Arguments = ($nativeArgs | ForEach-Object { Quote-NativeArgument $_ }) -join ' '
    $start.UseShellExecute = $false  # Inherit the MCP / Hook handles without a text pipeline.
    $child = [Diagnostics.Process]::Start($start)
    try { $child.WaitForExit(); $code = $child.ExitCode } finally { $child.Dispose() }
    exit $code
} catch {
    [Console]::Error.WriteLine('Seywo [runtime_unavailable]: cannot start the Codex workspace runtime.')
    exit 1
}
