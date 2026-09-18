# Deploys the container to Azure Container Apps (Consumption) inside the monthly free grant.
# Prereqs: az login; image already pushed to ghcr.io by the GitHub Actions workflow.
param(
  [string]$Rg = "cua-rg",
  [string]$Loc = "eastus",
  [string]$Env = "cua-env",
  [string]$App = "cua-demo",
  [string]$Image = "ghcr.io/<your-github-user>/cua-capability-engine:latest",
  [Parameter(Mandatory=$true)][string]$AnthropicKey,
  [string]$DiscoveryToken = ""
)
$ErrorActionPreference = "Stop"
az group create -n $Rg -l $Loc | Out-Null
az containerapp env create -n $Env -g $Rg -l $Loc | Out-Null
$exists = az containerapp show -n $App -g $Rg 2>$null
if (-not $exists) {
  az containerapp create -n $App -g $Rg --environment $Env --image $Image `
    --target-port 7860 --ingress external --transport auto `
    --cpu 1.0 --memory 2.0Gi --min-replicas 0 --max-replicas 1 `
    --secrets "anthropic-key=$AnthropicKey" "discovery-token=$DiscoveryToken" `
    --env-vars "ANTHROPIC_API_KEY=secretref:anthropic-key" "CUA_DISCOVERY_TOKEN=secretref:discovery-token" `
               "CUA_HEADLESS=false" "CUA_MODEL=claude-sonnet-5"
} else {
  az containerapp update -n $App -g $Rg --image $Image
}
$fqdn = az containerapp show -n $App -g $Rg --query properties.configuration.ingress.fqdn -o tsv
Write-Host "Live at https://$fqdn   (add '$fqdn' to policies/default.yaml allowed_hosts and redeploy)"
