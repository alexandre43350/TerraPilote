#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Supprime les profils utilisateurs (dossier C:\Users + registre) SAUF :
      - les comptes locaux de la machine
      - les profils systeme (Special)
      - la session en cours
.EXAMPLE
    .\1_Supprimer-ProfilsUtilisateurs.ps1 -WhatIf   # simulation
    .\1_Supprimer-ProfilsUtilisateurs.ps1           # suppression reelle
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$sidsLocaux = (Get-LocalUser).SID.Value
$sidCourant = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

$profils = Get-CimInstance Win32_UserProfile | Where-Object {
    -not $_.Special -and
    $_.SID -notin $sidsLocaux -and
    $_.SID -ne $sidCourant
}

if (-not $profils) {
    Write-Host "Aucun profil a supprimer." -ForegroundColor Yellow
    return
}

foreach ($p in $profils) {
    if ($p.Loaded) {
        Write-Warning "Profil charge (session ouverte), ignore : $($p.LocalPath)"
        continue
    }
    if ($PSCmdlet.ShouldProcess($p.LocalPath, "Supprimer le profil")) {
        try {
            Remove-CimInstance -InputObject $p -ErrorAction Stop
            Write-Host "Supprime : $($p.LocalPath)" -ForegroundColor Green
        } catch {
            Write-Warning "Echec : $($p.LocalPath) - $($_.Exception.Message)"
        }
    }
}
