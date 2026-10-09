#Requires -Version 5.1
<#
.SYNOPSIS
    Starts the Sony a6000 camrig instance: usbipd auto-attach plus the server in WSL2.
.DESCRIPTION
    The a6000 backend runs inside the WSL Ubuntu distro (gphoto2 needs libusb,
    which usbipd provides; nothing on the Windows side can drive PTP). This
    script does the two things that have to happen on the Windows side:

      1. Keep the camera attached to WSL. `usbipd attach --auto-attach` stays
         running and re-attaches every time the body re-enumerates -- which it
         does whenever it sleeps and is woken, because a sleeping a6000 shows
         up as a USB HID device (054c:0994) and comes back as the PTP identity
         (054c:094e) only when woken by hand.
      2. Run server.py in WSL on the local port that settings.psd1 in the
         home_server repo publishes (Port = 8031 there, ServePort = 15000).

    Captures land in sony\captures on the Windows side so File Browser sees
    them. Run once from a normal (non-elevated) PowerShell; Ctrl+C stops both.
.EXAMPLE
    .\sony\start.ps1
.EXAMPLE
    .\sony\start.ps1 -Port 8031 -Keepalive 5
#>
[CmdletBinding()]
param(
    [int]    $Port = 8031,
    [double] $Keepalive = 5.0,
    [string] $Distro = 'Ubuntu',
    [string] $Venv = '/home/ericx/camrig-venv',
    [string] $LogLevel = 'info'
)

$ErrorActionPreference = 'Stop'

# The server is owner-gated (osmo\camrig\gate.py, osmo\GATE.md, home_server #154): with
# HOME_OWNER unset it answers 403 to everything, loopback included. Refuse before any side
# effect (usbipd, the port check) rather than start a camera nobody can reach.
$homeOwner = "$env:HOME_OWNER".Trim()
if (-not $homeOwner) {
    throw "HOME_OWNER is not set. The Sony server is owner-only and answers 403 to everyone without it. Set it to your tailnet login first, e.g. `$env:HOME_OWNER = 'you@example.com' (the login in 'tailscale status'), then re-run."
}
$env:HOME_OWNER = $homeOwner

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir
$captures = Join-Path $scriptDir 'captures'
if (-not (Test-Path $captures)) { New-Item -ItemType Directory -Path $captures | Out-Null }

# Refuse up front if the port is taken. uvicorn only discovers this after the
# backend has opened, and the failure then tears down the auto-attach we just
# started -- leaving the camera detached and the *other* instance still running.
$taken = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($taken) {
    $owner = ($taken | Select-Object -First 1).OwningProcess
    $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
    throw "127.0.0.1:$Port is already in use (pid $owner, $name). Another camrig instance is probably running -- stop it, or pass -Port."
}

function To-WslPath([string] $winPath) {
    $full = (Resolve-Path $winPath).Path
    $drive = $full.Substring(0, 1).ToLower()
    return '/mnt/' + $drive + $full.Substring(2).Replace('\', '/')
}

# -- 1. the camera --------------------------------------------------------
# Only the PC Remote identity (094e) is shared with usbipd. The HID identity a
# sleeping body presents (0994) is deliberately not: attaching it does nothing
# useful, and binding it needs an elevated shell.
$listing = usbipd list 2>$null
$line = $listing | Select-String '054c:094e' | Select-Object -First 1
$attacher = $null
if ($line) {
    $busid = ($line.ToString().Trim() -split '\s+')[0]
    Write-Host "a6000 (PC Remote) on bus $busid -- starting usbipd auto-attach"
    $attacher = Start-Process -FilePath 'usbipd' `
        -ArgumentList @('attach', '--wsl', '--busid', $busid, '--auto-attach') `
        -WindowStyle Hidden -PassThru
} else {
    $sleeping = $listing | Select-String '054c:0994'
    if ($sleeping) {
        Write-Warning "the a6000 is asleep (HID identity). Wake it with a half-press; the server will reconnect on its own once it is attached."
        Write-Warning "auto-attach was NOT started because the PTP identity is not present yet -- re-run this script once the camera is awake, or run: usbipd attach --wsl --busid <busid> --auto-attach"
    } else {
        Write-Warning "no a6000 found in 'usbipd list'. Plug it in with USB Connection = PC Remote."
    }
}

# -- 2. the server --------------------------------------------------------
$server   = To-WslPath (Join-Path $repo 'osmo\server.py')
$capWsl   = To-WslPath $captures
$python   = "$Venv/bin/python"
$cmd = "$python $server --backend sony --host 127.0.0.1 --port $Port --keepalive $Keepalive --captures '$capWsl' --log-level $LogLevel"

# Windows environment variables do not reach WSL unless WSLENV lists them; /u = Windows to WSL
# only. HOME_OWNER is required (checked above). HOME_TAILNET_NAME is passed when set: the gate
# otherwise finds the tailnet name by running Windows' tailscale.exe through WSL interop.
$savedWslEnv = $env:WSLENV
$forward = @('HOME_OWNER/u')
if ("$env:HOME_TAILNET_NAME".Trim()) { $forward += 'HOME_TAILNET_NAME/u' }
$env:WSLENV = (@($savedWslEnv, ($forward -join ':')) | Where-Object { $_ }) -join ':'

Write-Host "starting camrig (sony) in WSL on 127.0.0.1:$Port (owner gate: $homeOwner)"
Write-Host "  $cmd"
try {
    wsl -d $Distro -- bash -lc $cmd
} finally {
    $env:WSLENV = $savedWslEnv
    if ($attacher -and -not $attacher.HasExited) {
        Write-Host "stopping usbipd auto-attach"
        Stop-Process -Id $attacher.Id -Force -ErrorAction SilentlyContinue
    }
}
