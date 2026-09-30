param namePrefix string
param location string
param tags object
@description('VNet address space, /24 or larger. Subnets are carved from its first /24.')
param addressPrefix string

var appSubnetPrefix = cidrSubnet(addressPrefix, 26, 0)
var endpointSubnetPrefix = cidrSubnet(addressPrefix, 27, 2)
var harvesterSubnetPrefix = cidrSubnet(addressPrefix, 27, 3)

// App integration and private endpoint subnets: inbound only from inside the VNet.
resource baselineNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${namePrefix}-baseline'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowVnetInbound'
        properties: {
          priority: 100
          access: 'Allow'
          direction: 'Inbound'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          destinationAddressPrefix: 'VirtualNetwork'
        }
      }
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 4096
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

// Harvester: no inbound at all (admin is Azure Run Command); outbound HTTPS only.
// NSGs cannot filter by host name; the harvester's HTTP client enforces its own feed allowlist.
resource harvesterNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${namePrefix}-harvester'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'DenyAllInbound'
        properties: {
          priority: 100
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
      {
        name: 'AllowAzureWireServer'
        properties: {
          priority: 110
          access: 'Allow'
          direction: 'Outbound'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRanges: [
            '80'
            '32526'
          ]
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '168.63.129.16'
        }
      }
      {
        name: 'AllowPrivateEndpointHttps'
        properties: {
          priority: 120
          access: 'Allow'
          direction: 'Outbound'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: endpointSubnetPrefix
        }
      }
      {
        name: 'AllowInternetHttps'
        properties: {
          priority: 130
          access: 'Allow'
          direction: 'Outbound'
          protocol: 'Tcp'
          sourcePortRange: '*'
          destinationPortRange: '443'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: 'Internet'
        }
      }
      {
        name: 'DenyOtherOutbound'
        properties: {
          priority: 4096
          access: 'Deny'
          direction: 'Outbound'
          protocol: '*'
          sourcePortRange: '*'
          destinationPortRange: '*'
          sourceAddressPrefix: '*'
          destinationAddressPrefix: '*'
        }
      }
    ]
  }
}

// Outbound for the harvester subnet only, so the VM needs no public IP.
resource natIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${namePrefix}-nat'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'ng-${namePrefix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: natIp.id
      }
    ]
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${namePrefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        addressPrefix
      ]
    }
    subnets: [
      {
        name: 'snet-app'
        properties: {
          addressPrefix: appSubnetPrefix
          networkSecurityGroup: {
            id: baselineNsg.id
          }
          // The API's access restriction identifies the web app's calls by this service endpoint.
          serviceEndpoints: [
            {
              service: 'Microsoft.Web'
            }
          ]
          delegations: [
            {
              name: 'app-service'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
        }
      }
      {
        name: 'snet-private-endpoints'
        properties: {
          addressPrefix: endpointSubnetPrefix
          defaultOutboundAccess: false
          // Private endpoints ignore NSGs unless this is set.
          privateEndpointNetworkPolicies: 'NetworkSecurityGroupEnabled'
          networkSecurityGroup: {
            id: baselineNsg.id
          }
        }
      }
      {
        name: 'snet-harvester'
        properties: {
          addressPrefix: harvesterSubnetPrefix
          defaultOutboundAccess: false
          networkSecurityGroup: {
            id: harvesterNsg.id
          }
          natGateway: {
            id: natGateway.id
          }
        }
      }
    ]
  }
}

resource blobZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource vaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource blobZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobZone
  name: vnet.name
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource vaultZoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: vaultZone
  name: vnet.name
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

output vnetId string = vnet.id
// By name: ARM does not guarantee the order of properties.subnets.
output appSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-app')
output endpointSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-private-endpoints')
output harvesterSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'snet-harvester')
output blobZoneId string = blobZone.id
output vaultZoneId string = vaultZone.id
output natPublicIp string = natIp.properties.ipAddress
