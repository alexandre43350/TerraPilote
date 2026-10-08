#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Joint le poste au domaine lachartreuse.loc puis redemarre.
    Le mot de passe de winadmin est demande a l'execution.
#>
param(
    [string]$Domaine = "lachartreuse.loc",
    [string]$Compte  = "winadmin@lachartreuse.loc",
    [System.Management.Automation.PSCredential]$Cred = (Get-Credential -UserName $Compte -Message "Admin domaine")
)

if ((Get-CimInstance Win32_ComputerSystem).PartOfDomain) {
    Write-Host "Le poste est deja dans un domaine." -ForegroundColor Yellow
    return
}

Add-Computer -DomainName $Domaine -Credential $Cred -Force -Restart
