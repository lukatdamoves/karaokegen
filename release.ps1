# Publish KaraokeGen.exe as a GitHub Release.
# One-time setup: install GitHub CLI (winget install GitHub.cli) and run:
#   gh auth login
# Then build the exe and publish:
#   .\build_exe.ps1 -OneFile
#   .\release.ps1            (or .\release.ps1 -Tag v1.0.1)
param([string]$Tag = "v1.0.0", [string]$Exe = "",
      [string]$Repo = "lukatdamoves/karaokegen")

$ErrorActionPreference = "Stop"

# Resolve the exe explicitly: a -OneFile build lands in dist\ (repo root may
# hold a stale onedir stub from a previous default build — never ship that by
# accident). dist first, root fallback, explicit -Exe override for anything else.
if (-not $Exe) {
  foreach ($cand in @("dist\KaraokeGen.exe", ".\KaraokeGen.exe")) {
    if (Test-Path $cand) { $Exe = $cand; break }
  }
}
if (-not $Exe -or -not (Test-Path $Exe)) {
  throw "No KaraokeGen.exe found. Build it first: .\build_exe.ps1 -OneFile"
}
$sizeMB = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host "Releasing $Exe (${sizeMB} MB) as $Tag"

$notes = @'
KaraokeGen desktop app for Windows.

Included files:
- KaraokeGen.exe (single file, no install, no Python needed; ffmpeg bundled)

Setup:
1. Download KaraokeGen.exe and double-click it.
2. On first launch hit "Save token & Deploy backend" (paste the
   Modal token command first) - the GPU backend deploys itself, no terminal needed.
3. Pick genre, upload audio, paste lyrics (or transcribe), align, render.

Notes:
- Settings live at %APPDATA%\KaraokeGen\config.json (survives updates).
- First launch can take a minute (Windows scans the file).
- The exe is unsigned, so SmartScreen asks for approval.
- Developers: manual `modal deploy modal_app.py` also works (see README).
'@

# --notes-file (not --notes): multiline text with quotes/backticks does not
# survive the native arg boundary intact on Windows, and gh errors out.
# Explicit --repo: this checkout has multiple remotes, so never guess.
# Check $LASTEXITCODE: a failed gh must never print a false "Released".
$notesFile = [System.IO.Path]::GetTempFileName()
try {
  Set-Content -Path $notesFile -Value $notes -Encoding UTF8
  gh release create $Tag $Exe .\.env.example --repo $Repo --title "KaraokeGen $Tag" --notes-file $notesFile
  if ($LASTEXITCODE -ne 0) { throw "gh release create failed (exit $LASTEXITCODE)" }
} finally {
  Remove-Item $notesFile -ErrorAction SilentlyContinue
}
Write-Host "Released $Tag"
