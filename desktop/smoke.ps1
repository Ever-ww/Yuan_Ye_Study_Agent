$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    npm.cmd --prefix ..\ui run build
    cargo check --manifest-path src-tauri\Cargo.toml

    if ($env:YY_TAURI_RUN_SMOKE -ne "1") {
        Write-Output "Tauri compile smoke passed. Set YY_TAURI_RUN_SMOKE=1 to launch the desktop window."
        exit 0
    }

    $process = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "tauri", "--", "dev") -WorkingDirectory (Get-Location) -PassThru -WindowStyle Hidden
    try {
        Start-Sleep -Seconds 12
        if ($process.HasExited) {
            throw "Tauri dev process exited early with code $($process.ExitCode)."
        }
        Write-Output "Tauri desktop process stayed alive for the smoke window."
    } finally {
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
    }
} finally {
    Pop-Location
}
