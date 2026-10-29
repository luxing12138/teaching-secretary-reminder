[CmdletBinding()]
param(
    [string]$BootstrapPython = 'python',
    [string]$OutputDirectory = 'release',
    [string]$DwsDirectory = ''
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Push-Location -LiteralPath $root
try {
    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $root $OutputDirectory))
    $running = @(Get-Process TeachingSecretaryReminder -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($releaseRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($running.Count) { throw '请先从托盘退出此输出目录中的提醒器。' }
    if (-not (Test-Path '.venv/Scripts/python.exe')) {
        & $BootstrapPython -m venv .venv
        if ($LASTEXITCODE) { throw '创建虚拟环境失败。' }
    }
    & '.venv/Scripts/python.exe' -m pip install --disable-pip-version-check pyinstaller -r requirements.txt
    if ($LASTEXITCODE) { throw '依赖安装失败。' }
    $buildArgs = @('--noconfirm', '--clean', '--noconsole', '--onedir', '--distpath', $releaseRoot,
        '--name', 'TeachingSecretaryReminder', '--collect-data', 'tzdata', '--paths', './src')
    if ($DwsDirectory) {
        $dwsRoot = (Resolve-Path -LiteralPath $DwsDirectory).Path
        if (-not (Test-Path (Join-Path $dwsRoot 'dws.exe'))) { throw 'DwsDirectory 中没有 dws.exe。' }
        $buildArgs += @('--add-binary', ((Join-Path $dwsRoot 'dws.exe') + ';tools'))
        foreach ($dll in Get-ChildItem -LiteralPath $dwsRoot -Filter '*.dll' -File) {
            $buildArgs += @('--add-binary', ($dll.FullName + ';tools'))
        }
    }
    & '.venv/Scripts/python.exe' -m PyInstaller @buildArgs './src/app.py'
    if ($LASTEXITCODE) { throw '可执行程序打包失败。' }
    Write-Output (Join-Path $releaseRoot 'TeachingSecretaryReminder/TeachingSecretaryReminder.exe')
} finally { Pop-Location }
