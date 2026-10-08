#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Sort le poste du domaine (retour au groupe de travail WORKGROUP) puis redemarre.
    Le mot de passe de winadmin est demande a l'execution.
#>
param(
    [string]$Compte = "winadmin@lachartreuse.loc",
    [System.Management.Automation.PSCredential]$Cred = (Get-Credential -UserName $Compte -Message "Admin domaine")
)

if (-not (Get-CimInstance Win32_ComputerSystem).PartOfDomain) {
    Write-Host "Le poste n'est pas dans un domaine." -ForegroundColor Yellow
    return
}

Remove-Computer -UnjoinDomainCredential $Cred -WorkgroupName "WORKGROUP" -Force -Restart
