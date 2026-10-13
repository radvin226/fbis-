@echo off
setlocal
cd /d "%~dp0"

where cloudflared >nul 2>nul
if not errorlevel 1 goto enable_panel

where winget >nul 2>nul
if errorlevel 1 (
  echo [ERROR] cloudflared and winget were not found.
  echo Install Cloudflare cloudflared, then run this file again.
  echo Official project: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
  pause
  exit /b 1
)

echo Installing Cloudflare cloudflared...
winget install -e --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
  echo [ERROR] cloudflared installation failed. Install it manually, then run this file again.
  pause
  exit /b 1
)

where cloudflared >nul 2>nul
if errorlevel 1 (
  echo.
  echo Installation completed. Close this window, open a new terminal, then run this file again.
  pause
  exit /b 0
)

:enable_panel
if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example.
  ) else (
    echo [ERROR] .env.example was not found.
    pause
    exit /b 1
  )
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='.env'; $t=[IO.File]::ReadAllText($p); if($t -match '(?m)^FBI_WEB_PANEL_TUNNEL='){$t=[regex]::Replace($t,'(?m)^FBI_WEB_PANEL_TUNNEL=.*$','FBI_WEB_PANEL_TUNNEL=1')}else{$t += [Environment]::NewLine+'FBI_WEB_PANEL_TUNNEL=1'+[Environment]::NewLine}; [IO.File]::WriteAllText($p,$t,[Text.UTF8Encoding]::new($false))"
if errorlevel 1 (
  echo [ERROR] Could not update .env.
  pause
  exit /b 1
)

echo.
echo [OK] Public Quick Tunnel is enabled in .env.
echo Check FBI_PHONE, FBI_SUDOS and other login settings in .env before starting the bot.
echo Start/restart the bot with: python main.py
echo Keep the private link sent by /owner or the group menu secure. The public URL may change after restart.
pause
