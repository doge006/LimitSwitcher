# Builds the Windows installer, build\LimitSwitcher-Setup.exe, from build\LimitSwitcher\ (the exe,
# the app and its own Python). Needs Python 3.13 x64 on PATH (for pip), the MSVC tools (cl, rc)
# on PATH for the exe, and Inno Setup 6. Used by .github/workflows/release.yml.
param([string]$Out = "build")
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$pyver = (python -c "import platform; print(platform.python_version())").Trim()
if (-not $pyver.StartsWith('3.13')) { throw "Python 3.13 is needed to build (found $pyver)" }
$app = Join-Path $Out 'LimitSwitcher'
Remove-Item -LiteralPath $Out -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $app | Out-Null

# 1. Python: the official embeddable package, with site-packages turned on.
$runtime = Join-Path $app 'runtime'
$embed = Join-Path $Out "python-$pyver-embed-amd64.zip"
Invoke-WebRequest "https://www.python.org/ftp/python/$pyver/python-$pyver-embed-amd64.zip" -OutFile $embed
Expand-Archive -LiteralPath $embed -DestinationPath $runtime
Remove-Item -LiteralPath $embed
$pth = Get-ChildItem -LiteralPath $runtime -Filter 'python3*._pth' | Select-Object -First 1
$zipName = (Get-ChildItem -LiteralPath $runtime -Filter 'python3*.zip' | Select-Object -First 1).Name
Set-Content -LiteralPath $pth.FullName -Value @($zipName, '.', 'Lib\site-packages', 'import site') -Encoding ascii

# 2. The app's packages (Windows only: Pillow, pystray).
python -m pip install --quiet --disable-pip-version-check --no-compile --only-binary=:all: `
    --target (Join-Path $runtime 'Lib\site-packages') -r requirements-native.txt

# 3. The app itself.
Copy-Item -Recurse -LiteralPath account_switcher -Destination $app
Get-ChildItem -LiteralPath (Join-Path $app 'account_switcher') -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Copy-Item -LiteralPath LimitSwitcher.pyw, README.md, LICENSE, THIRD-PARTY-NOTICES.txt -Destination $app

# 4. LimitSwitcher.exe: runs the bundled Python in its own process (Task Manager shows LimitSwitcher),
#    with the app icon and file details.
$version = (python -c "import sys; sys.path.insert(0, '.'); from account_switcher.version import VERSION; print(VERSION)").Trim()
rc /nologo /fo (Join-Path $Out 'launcher.res') scripts\win_launcher.rc
if ($LASTEXITCODE -ne 0) { throw 'rc failed' }
cl /nologo /O2 /W3 /DUNICODE /D_UNICODE /Brepro scripts\win_launcher.c (Join-Path $Out 'launcher.res') `
    /Fo"$Out\\" /Fe"$app\LimitSwitcher.exe" /link /SUBSYSTEM:WINDOWS /Brepro user32.lib shell32.lib
if (-not (Test-Path -LiteralPath (Join-Path $app 'LimitSwitcher.exe'))) { throw 'the launcher did not build' }
# The same exe every release (win_launcher.rc): say so when it has changed, since antivirus vendors
# then see a new file (scripts\win_launcher.sha256 is the one they were sent).
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $app 'LimitSwitcher.exe')).Hash.ToLower()
$known = (Get-Content -LiteralPath scripts\win_launcher.sha256 -ErrorAction SilentlyContinue | Select-Object -First 1)
if ($known -and $hash -eq $known.Trim()) { Write-Host "LimitSwitcher.exe is the known build ($hash)" }
else { Write-Host "::warning::LimitSwitcher.exe is a new build ($hash, known: $known): antivirus vendors see a new file" }
Remove-Item -LiteralPath (Join-Path $Out 'launcher.res'), (Join-Path $Out 'win_launcher.obj') -ErrorAction SilentlyContinue

# 5. The installer (scripts\LimitSwitcher.iss).
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe' }
if (-not (Test-Path -LiteralPath $iscc)) { throw 'Inno Setup 6 is needed (choco install innosetup)' }
& $iscc /Qp "/DAppVersion=$version" "/DSourceDir=$((Resolve-Path $app).Path)" "/O$((Resolve-Path $Out).Path)" scripts\LimitSwitcher.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed ($LASTEXITCODE)" }
$setup = Join-Path $Out 'LimitSwitcher-Setup.exe'
$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "Built $setup ($size MB), LimitSwitcher $version, Python $pyver"
