# Create a Tepat desktop shortcut with the tepat icon (user decision
# 2026-10-12). Called by create-shortcut.bat from the source folder.
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$here = Get-Location
$lnk = $ws.CreateShortcut((Join-Path $desktop 'Tepat.lnk'))
$lnk.TargetPath = (Join-Path $here 'start.bat')
$lnk.WorkingDirectory = $here.Path
$lnk.IconLocation = (Join-Path $here 'assets\icon.ico')
$lnk.Description = 'Tepat - Malay language checker (local server)'
$lnk.WindowStyle = 7   # minimized (the tray icon is the real entry point)
$lnk.Save()
Write-Host '[tepat] shortcut created on your desktop.'
