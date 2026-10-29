[CmdletBinding()]
param([string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$releaseExe = Join-Path $root 'release/TeachingSecretaryReminder/TeachingSecretaryReminder.exe'
if (Test-Path -LiteralPath $releaseExe) {
    $action = '"{0}" --sync-service' -f $releaseExe
} else {
    if (-not $PythonPath) { $PythonPath = Join-Path $root '.venv/Scripts/pythonw.exe' }
    if (-not (Test-Path -LiteralPath $PythonPath)) { throw '请先安装项目环境或通过 -PythonPath 指定 pythonw.exe。' }
    $action = '"{0}" "{1}"' -f $PythonPath, (Join-Path $root 'src/sync_service.py')
}
$failed = @()
& schtasks.exe /Create /TN 'TeachingSecretaryReminder-AtLogon' /SC ONLOGON /TR $action /RL LIMITED /IT /F | Out-Null
if ($LASTEXITCODE) { $failed += 'TeachingSecretaryReminder-AtLogon' }
& schtasks.exe /Create /TN 'TeachingSecretaryReminder-Every5Min' /SC MINUTE /MO 5 /TR $action /RL LIMITED /IT /F | Out-Null
if ($LASTEXITCODE) { $failed += 'TeachingSecretaryReminder-Every5Min' }
if ($failed.Count) { throw "计划任务安装失败：$($failed -join ', ')。请检查当前用户权限。" }
Write-Output '已安装本项目的登录启动与每五分钟检查任务。'
