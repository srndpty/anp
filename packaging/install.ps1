<#
.SYNOPSIS
    ビルド済みの anp を Program Files へ入れ、スタートメニューに登録する。

.DESCRIPTION
    Program Files への書き込みには管理者権限が要るので、**管理者として起動した
    PowerShell から実行する**。権限が無ければ何もせずに終わる。

    先に `uv run --group build pyinstaller packaging/anp.spec --noconfirm` で
    dist\anp を作っておくこと。

    PDF の関連付けはここでは行わない。あちらは利用者ごと（HKCU）の設定で
    管理者権限が要らないため、`register-pdf.ps1` に分けてある。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\install.ps1
#>
[CmdletBinding()]
param(
    # PyInstaller の出力（dist\anp）。既定はこのスクリプトから見た相対位置。
    [string] $Source = (Join-Path $PSScriptRoot "..\dist\anp"),

    [string] $Destination = "$env:ProgramFiles\anp"
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "管理者権限が必要です。PowerShell を「管理者として実行」してから、もう一度実行してください。"
}

if (-not (Test-Path (Join-Path $Source "anp.exe"))) {
    throw "$Source に anp.exe がありません。先にビルドしてください。"
}

# 起動中だと上書きできない。閉じてもらう（黙って kill はしない。読書位置と
# 学習マークの保存は終了時に走る）。
if (Get-Process -Name "anp" -ErrorAction SilentlyContinue) {
    throw "anp が起動中です。終了してから、もう一度実行してください。"
}

Write-Host "インストール先: $Destination"
if (Test-Path $Destination) {
    # 前の版のファイルが混ざらないよう、いったん消してから入れ直す。
    # 学習マークと設定は %LOCALAPPDATA%\anp にあるので、ここを消しても消えない。
    Remove-Item -Recurse -Force $Destination
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -Recurse -Force (Join-Path $Source "*") $Destination

$exe = Join-Path $Destination "anp.exe"
$startMenu = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\anp.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($startMenu)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $Destination
$shortcut.IconLocation = "$exe,0"
$shortcut.Description = "学習向け PDF リーダー"
$shortcut.Save()

Write-Host "完了しました。"
Write-Host "  実行ファイル : $exe"
Write-Host "  スタートメニュー : $startMenu"
Write-Host ""
Write-Host "PDF の関連付けは、管理者ではない通常の PowerShell から次を実行してください。"
Write-Host "  powershell -ExecutionPolicy Bypass -File packaging\register-pdf.ps1"
