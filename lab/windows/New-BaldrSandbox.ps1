param(
  [string]$Repository = (Resolve-Path "$PSScriptRoot\..\.."),
  [string]$Output = "$PSScriptRoot\baldr-router-clean.wsb"
)

$escaped = [System.Security.SecurityElement]::Escape($Repository)
$content = @"
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$escaped</HostFolder>
      <SandboxFolder>C:\BaldrRouter</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Enable</ClipboardRedirection>
  <LogonCommand>
    <Command>powershell.exe -ExecutionPolicy Bypass -File C:\BaldrRouter\lab\windows\bootstrap-sandbox.ps1</Command>
  </LogonCommand>
</Configuration>
"@
Set-Content -Path $Output -Value $content -Encoding UTF8
Write-Host "Generated $Output"
Write-Host "Open it to validate a clean Windows host installation. WSL scenarios require a VM with WSL enabled."
