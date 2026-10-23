// Mega Admin MCP on Azure App Service (Linux, Python). One file, one command.
//   az deployment group create -g <rg> -f deploy/bicep/main.bicep -p appName=<unique> tenantId=<tid> clientId=<cid> clientSecret=<secret>
targetScope = 'resourceGroup'

@description('Globally unique App Service name, becomes https://<appName>.azurewebsites.net')
@minLength(3)
@maxLength(40)
param appName string

@description('Azure region')
param location string = resourceGroup().location

@description('App Service plan SKU. B1 is enough for a team; use P0v3 or above for production. F1 is for a quick look only (no Always On, sleeps when idle).')
@allowed(['F1', 'B1', 'B2', 'B3', 'P0v3', 'P1v3', 'P2v3', 'P1mv3', 'S1'])
param sku string = 'B1'

@description('delegated (Copilot Studio OAuth, recommended) | apikey (server identity) | none (never on the internet)')
@allowed(['delegated', 'apikey', 'none'])
param authMode string = 'delegated'

@description('Entra tenant id')
param tenantId string

@description('App registration (client) id created by scripts/setup-entra.ps1')
param clientId string = ''

@secure()
@description('App registration client secret (leave empty when using the managed identity federated credential)')
param clientSecret string = ''

@description('Use the App Service managed identity as the federated credential of the app registration instead of a secret')
param useManagedIdentityFederatedCredential bool = false

@secure()
@description('API key when authMode = apikey')
param apiKey string = ''

@description('Turn every write operation off')
param readOnly bool = false

@description('Default Azure subscription id for FinOps tools (optional)')
param defaultSubscriptionId string = ''

@description('Deploy the code straight from the public GitHub repository (Oryx build). Set false when you deploy a zip or container yourself.')
param deployFromGitHub bool = true

@description('GitHub repository URL to deploy from')
param repoUrl string = 'https://github.com/crowdedLeopard/mega-admin-mcp'

@description('Branch to deploy')
param branch string = 'main'

@description('Python runtime')
param pythonVersion string = '3.13'

var publicBaseUrl = 'https://${appName}.azurewebsites.net'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'linux'
  sku: {
    name: sku
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: 'PYTHON|${pythonVersion}'
      appCommandLine: 'python run.py'
      alwaysOn: sku != 'F1'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/health'
      httpLoggingEnabled: true
      detailedErrorLoggingEnabled: true
      logsDirectorySizeLimit: 50
      autoHealEnabled: true
      autoHealRules: {
        triggers: {
          statusCodes: [
            {
              status: 500
              subStatus: 0
              count: 20
              timeInterval: '00:05:00'
            }
          ]
          slowRequests: {
            timeTaken: '00:02:00'
            count: 30
            timeInterval: '00:05:00'
          }
        }
        actions: {
          actionType: 'Recycle'
          minProcessExecutionTime: '00:02:00'
        }
      }
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'PORT', value: '8000' }
        { name: 'PYTHONDONTWRITEBYTECODE', value: '1' }
        { name: 'AUTH_MODE', value: authMode }
        { name: 'TENANT_ID', value: tenantId }
        { name: 'CLIENT_ID', value: clientId }
        { name: 'CLIENT_SECRET', value: clientSecret }
        { name: 'USE_MANAGED_IDENTITY_FIC', value: string(useManagedIdentityFederatedCredential) }
        { name: 'MCP_API_KEY', value: apiKey }
        { name: 'READ_ONLY', value: string(readOnly) }
        { name: 'DEFAULT_SUBSCRIPTION_ID', value: defaultSubscriptionId }
        { name: 'PUBLIC_BASE_URL', value: publicBaseUrl }
        { name: 'CATALOG_AUTO_REFRESH', value: 'true' }
        { name: 'CATALOG_REFRESH_HOURS', value: '24' }
        { name: 'LOG_LEVEL', value: 'INFO' }
      ]
    }
  }
}

resource sourceControl 'Microsoft.Web/sites/sourcecontrols@2023-12-01' = if (deployFromGitHub) {
  parent: site
  name: 'web'
  properties: {
    repoUrl: repoUrl
    branch: branch
    isManualIntegration: true
    deploymentRollbackEnabled: false
    isMercurial: false
  }
}

output url string = publicBaseUrl
output mcpEndpoint string = '${publicBaseUrl}/mcp'
output healthEndpoint string = '${publicBaseUrl}/health'
output connectorDefinition string = '${publicBaseUrl}/connector.yaml'
output managedIdentityPrincipalId string = site.identity.principalId
output nextSteps string = 'In Copilot Studio add an MCP tool with Server URL ${publicBaseUrl}/mcp, OAuth 2.0 Manual, scopes api://${clientId}/.default. Then add the wizard redirect URL to the app registration.'
