<#
.SYNOPSIS
  Sentinel — single-command Windows installer (M11-DIST; the PowerShell peer of install.sh).
.DESCRIPTION
  Downloads the `agentctl.exe` binary from the latest GitHub Release, verifies its checksum (hard fail on
  mismatch) and — if `cosign` is on PATH — its keyless signature (pinned identity), then installs it to
  %LOCALAPPDATA%\Programs\sentinel (no admin). For a full explore run use Docker Desktop / WSL (see
  docs/QUICKSTART.md); for an air-gapped host use the offline bundle (docs/DISTRIBUTION.md §6).

    iwr -useb https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.ps1 | iex

  Env overrides (also used by the CI install-smoke against a locally-built fake release):
    SENTINEL_VERSION  SENTINEL_BIN_DIR  SENTINEL_BASE_URL  SENTINEL_API_URL  SENTINEL_REPO
    SENTINEL_COSIGN_ID_RE / SENTINEL_COSIGN_ISSUER  (override the pinned keyless verify identity)
#>
#Requires -Version 5
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-EnvOr($name, $default) {
  $v = [Environment]::GetEnvironmentVariable($name)
  if ($v) { $v } else { $default }
}

$repo    = Get-EnvOr 'SENTINEL_REPO'     'AlexGromer/sentinel'
$binDir  = Get-EnvOr 'SENTINEL_BIN_DIR'  (Join-Path $env:LOCALAPPDATA 'Programs\sentinel')
$version = Get-EnvOr 'SENTINEL_VERSION'  'latest'
$baseUrl = Get-EnvOr 'SENTINEL_BASE_URL' "https://github.com/$repo/releases/download"
$apiUrl  = Get-EnvOr 'SENTINEL_API_URL'  "https://api.github.com/repos/$repo/releases/latest"
$idRe    = Get-EnvOr 'SENTINEL_COSIGN_ID_RE' 'https://github.com/AlexGromer/sentinel/.github/workflows/release.yml@refs/tags/v.*'
$issuer  = Get-EnvOr 'SENTINEL_COSIGN_ISSUER' 'https://token.actions.githubusercontent.com'

function Info($m) { Write-Host "== $m" -ForegroundColor Cyan }
# throw (NOT exit): via `iwr | iex` the script runs in the CALLER'S runspace, so a bare `exit` would kill
# the user's whole PowerShell session. An uncaught throw exits non-zero under `-File` (the CI smoke) yet
# only surfaces an error under iex without terminating the session.
function Die($m)  { throw $m }

$tmp = $null
try {
  # --- platform (arch) --------------------------------------------------------------------------
  $arch = switch ($env:PROCESSOR_ARCHITECTURE) {
    'AMD64' { 'amd64' }
    'ARM64' { 'arm64' }
    default { Die "unsupported arch '$($env:PROCESSOR_ARCHITECTURE)'" }
  }
  if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { Die "tar.exe is required (Windows 10 1803+ / Windows 11)" }

  # --- resolve version --------------------------------------------------------------------------
  if ($version -eq 'latest') {
    Info "resolving the latest release ($repo)"
    $version = (Invoke-RestMethod -Uri $apiUrl -Headers @{ 'User-Agent' = 'sentinel-install' }).tag_name
    if (-not $version) { Die "could not resolve the latest release tag from $apiUrl (network/rate-limit, or no release cut yet)" }
  }
  Info "installing Sentinel agentctl $version (windows/$arch)"

  # --- download ---------------------------------------------------------------------------------
  $tmp = Join-Path $env:TEMP ("sentinel-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $archive = "sentinel-$version-windows-$arch.tar.gz"
  $base = "$baseUrl/$version"
  Info "downloading $archive + checksums"
  Invoke-WebRequest -Uri "$base/$archive"         -OutFile (Join-Path $tmp $archive)          -UseBasicParsing
  Invoke-WebRequest -Uri "$base/checksums.sha256" -OutFile (Join-Path $tmp 'checksums.sha256') -UseBasicParsing

  # --- checksum (hard fail) ---------------------------------------------------------------------
  Info "verifying checksum"
  $line = Select-String -Path (Join-Path $tmp 'checksums.sha256') -SimpleMatch $archive | Select-Object -First 1
  if (-not $line) { Die "checksum entry for $archive not found — refusing to install" }
  $want = ($line.Line -split '\s+')[0].ToLower()
  $got  = (Get-FileHash -Algorithm SHA256 -Path (Join-Path $tmp $archive)).Hash.ToLower()
  if ($got -ne $want) { Die "checksum verification FAILED for $archive (want $want, got $got) — refusing to install" }

  # --- cosign signature (optional; pinned keyless identity) -------------------------------------
  if (Get-Command cosign -ErrorAction SilentlyContinue) {
    $bundle = Join-Path $tmp "$archive.cosign.bundle"
    $haveBundle = $true
    try { Invoke-WebRequest -Uri "$base/$archive.cosign.bundle" -OutFile $bundle -UseBasicParsing } catch { $haveBundle = $false }
    if ($haveBundle) {
      Info "verifying Cosign signature"
      & cosign verify-blob (Join-Path $tmp $archive) --bundle $bundle --certificate-identity-regexp $idRe --certificate-oidc-issuer $issuer
      if ($LASTEXITCODE -ne 0) { Die "Cosign verification FAILED for $archive" }
    } else { Write-Warning "no .cosign.bundle for $archive — skipping signature check" }
  } else { Write-Warning "cosign not installed — skipping signature verification (install cosign v3+ for supply-chain assurance)" }

  # --- extract + install (hard gate) ------------------------------------------------------------
  & tar.exe -xzf (Join-Path $tmp $archive) -C $tmp
  if ($LASTEXITCODE -ne 0) { Die "failed to extract $archive" }
  $src = Get-ChildItem -Path $tmp -Recurse -Filter 'agentctl.exe' | Select-Object -First 1
  if (-not $src) { Die "agentctl.exe not found inside $archive" }
  New-Item -ItemType Directory -Force -Path $binDir | Out-Null
  Copy-Item -Force -Path $src.FullName -Destination (Join-Path $binDir 'agentctl.exe')

  $got = & (Join-Path $binDir 'agentctl.exe') --version
  if ($LASTEXITCODE -ne 0) { Die "the installed agentctl.exe failed to run" }
  Info "installed: $binDir\agentctl.exe ($got)"
  if (($env:Path -split ';') -notcontains $binDir) {
    Write-Warning "$binDir is not on PATH — add it once:  setx PATH `"$binDir;`$env:Path`""
  }
  Write-Host ""
  Write-Host "Sentinel agentctl $version installed. A full explore run needs the Docker image (Docker Desktop/WSL)."
  Write-Host "  Full quickstart:  docs/QUICKSTART.md   ·   Air-gapped install:  docs/DISTRIBUTION.md §6"
}
catch {
  throw "install failed: $($_.Exception.Message)"   # re-throw: non-zero under -File, surfaced (not fatal) under iex
}
finally {
  if ($tmp -and (Test-Path $tmp)) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}
