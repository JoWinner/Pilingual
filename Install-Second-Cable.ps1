$ErrorActionPreference = 'Stop'
$installer = Join-Path $PSScriptRoot '.downloads\HiFiCable\HiFiCableAsioBridgeSetup.exe'
if (-not (Test-Path -LiteralPath $installer)) {
    throw 'Installer is missing. Download Hi-Fi Cable from https://vb-audio.com/Cable/index.htm and extract it into .downloads\HiFiCable.'
}
$signature = Get-AuthenticodeSignature -LiteralPath $installer
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Vincent Burel,') {
    throw 'Installer signature did not match the expected VB-Audio publisher. Installation stopped.'
}
Write-Host 'Opening the signed VB-Audio Hi-Fi Cable installer. Complete Install Driver and reboot if requested.'
Write-Host 'This adds a second virtual audio path. It does not remove the existing cable.'
Start-Process -FilePath $installer -Verb RunAs -WindowStyle Normal
