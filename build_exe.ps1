# Build KaraokeGen.exe (Windows, PyInstaller onedir + native window).
# Usage:  .\build_exe.ps1        # onedir build in dist\KaraokeGen\
#         .\build_exe.ps1 -OneFile  # single-file exe (slower to start)
param([switch]$OneFile)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

# 1. deps (pip prints its self-update notice on stderr -- judge by exit code,
# not by stderr noise, or every build trips $ErrorActionPreference = "Stop")
$prevEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"
pip install -r requirements.txt -r requirements-exe.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
$ErrorActionPreference = $prevEA

# 2. build (splatted args - no backtick continuations)
$mode = "--onedir"
if ($OneFile) { $mode = "--onefile" }
# build stamp: proves which source an exe came from (shown in the app header)
$stamp = @{ built = (Get-Date -Format "yyyy-MM-dd HH:mm"); commit = (git rev-parse --short HEAD) } | ConvertTo-Json
Set-Content -Path "local_app/static/build.json" -Value $stamp -Encoding UTF8
Write-Host "build stamp: $stamp"
$ffmpegBin = $null
foreach ($cand in @("C:\ffmpeg\ffmpeg.exe", "C:\ffmpeg\bin\ffmpeg.exe")) {
  if (Test-Path $cand) { $ffmpegBin = $cand; break }
}
$pyArgs = @(
  "--noconfirm", "--clean", $mode, "--windowed", "--name", "KaraokeGen",
  "--icon", "local_app/static/icon.ico",
  "--add-data", "local_app/templates;local_app/templates",
  "--add-data", "local_app/static;local_app/static",
  "--hidden-import", "uvicorn.logging",
  "--hidden-import", "uvicorn.loops.auto",
  "--hidden-import", "uvicorn.protocols.http.auto",
  "--hidden-import", "uvicorn.protocols.websockets.auto",
  "--hidden-import", "jinja2",
  "--hidden-import", "multipart",
  "--hidden-import", "dotenv",
  "--hidden-import", "httpx",
  "--hidden-import", "anyio",
  "--hidden-import", "local_app.server",
  "--hidden-import", "local_app.video",
  "local_app/exe_main.py"
)
if ($ffmpegBin) {
  $pyArgs += @("--add-binary", "$ffmpegBin;.")
  Write-Host "Bundling ffmpeg from $ffmpegBin"
} else {
  Write-Host "WARNING: ffmpeg.exe not found at C:\ffmpeg\ffmpeg.exe -- exe will require external ffmpeg"
}
# bundle the deploy toolchain so the one-click Setup Modal works without a source checkout
$pyArgs += @("--add-data", "modal_app.py;.")
$pyArgs += @("--add-data", "KaraokeGen;KaraokeGen")
foreach ($cand in @("LyricsAlignment-Multilingual", "$env:LOCALAPPDATA\KaraokeGen\LyricsAlignment-Multilingual")) {
  if (Test-Path $cand) { $pyArgs += @("--add-data", "$cand;LyricsAlignment-Multilingual"); Write-Host "Bundling $cand"; break }
}
$pyArgs += @("--hidden-import", "modal")
$pyArgs += @("--hidden-import", "modal.config")
# modal lazy-loads submodules at runtime (App.deploy, config lookup) that
# static analysis can miss — collect them so the frozen SDK-deploy path works
$pyArgs += @("--collect-submodules", "modal")
$pyArgs += @("--hidden-import", "zipfile")
$ErrorActionPreference = "Continue"
pyinstaller @pyArgs
if ($LASTEXITCODE -ne 0) { throw "pyinstaller build failed" }
$ErrorActionPreference = "Stop"

# 3. dist is the canonical build — mirror it to the repo root so both exes are identical
if (-not $OneFile) {
  Write-Host "Mirroring dist build to repo root..."
  Copy-Item "dist/KaraokeGen/KaraokeGen.exe" ".\KaraokeGen.exe" -Force
  if (Test-Path "dist/KaraokeGen/_internal") {
    if (Test-Path ".\_internal") { Remove-Item ".\_internal" -Recurse -Force }
    Copy-Item "dist/KaraokeGen/_internal" ".\_internal" -Recurse -Force
  }
  $outDir = "dist/KaraokeGen + .\KaraokeGen.exe (mirrored, identical)"
} else {
  $outDir = "dist"
}
Write-Host ""
Write-Host "Build done: $outDir"
if ($ffmpegBin) {
  Write-Host "ffmpeg bundled inside the exe (extracted to %LOCALAPPDATA%\KaraokeGen on first run)"
} else {
  Write-Host "Also place ffmpeg.exe next to KaraokeGen.exe (or on PATH) - it is NOT bundled."
}
Write-Host "Config lives at %APPDATA%\KaraokeGen\config.json -- set via the in-app gear icon."
