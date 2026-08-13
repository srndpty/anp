<#
.SYNOPSIS
    anp を PDF の「プログラムから開く」候補として登録する（利用者ごと）。

.DESCRIPTION
    書き込むのは HKCU\Software\Classes だけなので **管理者権限は要らない**。
    通常の PowerShell から実行する。

    Windows 10 以降、**既定のアプリそのものはプログラムから変更できない**
    （利用者が選んだ既定は UserChoice のハッシュで保護されていて、書き換えると
    OS に無効化される）。ここで行うのは候補として名乗り出るところまでで、
    既定にするかどうかは利用者が設定画面で決める。手順は最後に表示する。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\register-pdf.ps1
    powershell -ExecutionPolicy Bypass -File packaging\register-pdf.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string] $ExePath = "$env:ProgramFiles\anp\anp.exe",

    # 登録を取り消す。
    [switch] $Unregister
)

$ErrorActionPreference = "Stop"

# ファイルの種類の ID。表示名でも拡張子でもなく、これが設定の鍵になる。
$progId = "anp.pdf"
$classes = "HKCU:\Software\Classes"
$progIdKey = "$classes\$progId"
$appKey = "$classes\Applications\anp.exe"
$openWithKey = "$classes\.pdf\OpenWithProgids"

if ($Unregister) {
    foreach ($key in @($progIdKey, $appKey)) {
        if (Test-Path $key) { Remove-Item -Recurse -Force $key }
    }
    if (Test-Path $openWithKey) {
        Remove-ItemProperty -Path $openWithKey -Name $progId -ErrorAction SilentlyContinue
    }
    Write-Host "登録を取り消しました。"
    return
}

if (-not (Test-Path $ExePath)) {
    throw "$ExePath がありません。先に install.ps1 でインストールしてください。"
}

$command = "`"$ExePath`" `"%1`""

# ファイルの種類そのもの。開く手順とアイコンをここに書く。
New-Item -Force -Path "$progIdKey\shell\open\command" | Out-Null
Set-ItemProperty -Path $progIdKey -Name "(default)" -Value "PDF ドキュメント (anp)"
New-Item -Force -Path "$progIdKey\DefaultIcon" | Out-Null
Set-ItemProperty -Path "$progIdKey\DefaultIcon" -Name "(default)" -Value "$ExePath,0"
Set-ItemProperty -Path "$progIdKey\shell\open\command" -Name "(default)" -Value $command

# 「プログラムから開く」の一覧に anp を出すための登録。
New-Item -Force -Path "$appKey\shell\open\command" | Out-Null
Set-ItemProperty -Path "$appKey\shell\open\command" -Name "(default)" -Value $command
Set-ItemProperty -Path $appKey -Name "FriendlyAppName" -Value "anp"
New-Item -Force -Path "$appKey\SupportedTypes" | Out-Null
Set-ItemProperty -Path "$appKey\SupportedTypes" -Name ".pdf" -Value ""

# .pdf の候補一覧へ加える。**既定は書き換えない**（UserChoice は触らない）。
New-Item -Force -Path $openWithKey | Out-Null
Set-ItemProperty -Path $openWithKey -Name $progId -Value ([byte[]]@()) -Type None

# 変更をエクスプローラへ知らせる。再ログオンを待たずに一覧へ出るようにする。
$signature = @'
[DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, IntPtr item1, IntPtr item2);
'@
$shell32 = Add-Type -MemberDefinition $signature -Name "AnpShell" -Namespace "Anp" -PassThru
$SHCNE_ASSOCCHANGED = 0x08000000
$SHCNF_IDLIST = 0x0000
$shell32::SHChangeNotify($SHCNE_ASSOCCHANGED, $SHCNF_IDLIST, [IntPtr]::Zero, [IntPtr]::Zero)

Write-Host "登録しました（$progId → $ExePath）。"
Write-Host ""
Write-Host "既定のアプリにするには、次のどちらかを行ってください（OS の仕様で自動化できません）。"
Write-Host "  A. PDF を右クリック → プログラムから開く → 別のプログラムを選択 → anp →"
Write-Host "     「常に使う」を選ぶ"
Write-Host "  B. 設定 → アプリ → 既定のアプリ → anp → .pdf の既定を anp にする"
