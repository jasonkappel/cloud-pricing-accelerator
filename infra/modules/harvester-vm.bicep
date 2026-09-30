// On-demand price harvester: private NIC (egress via the subnet's NAT gateway), no inbound access,
// an independent data disk for raw feeds, and a user-assigned identity that can only write staging.
param name string
param location string
param tags object
param subnetId string
param identityId string
param vmSize string
@description('SSH public key only. No inbound path exists; administration is Azure Run Command.')
param adminSshPublicKey string
param adminUsername string = 'harvestadmin'
param dataDiskSizeGb int = 128

resource nic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: 'nic-${name}'
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'primary'
        properties: {
          primary: true
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: subnetId
          }
        }
      }
    ]
  }
}

// Survives VM deletion (deleteOption Detach) so collected feeds are never lost to a rebuild.
resource dataDisk 'Microsoft.Compute/disks@2024-03-02' = {
  name: 'disk-${name}-data'
  location: location
  tags: tags
  sku: {
    name: 'Premium_LRS'
  }
  properties: {
    creationData: {
      createOption: 'Empty'
    }
    diskSizeGB: dataDiskSizeGb
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    securityProfile: {
      securityType: 'TrustedLaunch'
      uefiSettings: {
        secureBootEnabled: true
        vTpmEnabled: true
      }
    }
    storageProfile: {
      imageReference: {
        publisher: 'Canonical'
        offer: 'ubuntu-24_04-lts'
        sku: 'server'
        version: 'latest'
      }
      osDisk: {
        name: 'disk-${name}-os'
        createOption: 'FromImage'
        deleteOption: 'Delete'
        diskSizeGB: 64
        managedDisk: {
          storageAccountType: 'Premium_LRS'
        }
      }
      dataDisks: [
        {
          lun: 0
          createOption: 'Attach'
          caching: 'None'
          deleteOption: 'Detach'
          managedDisk: {
            id: dataDisk.id
          }
        }
      ]
    }
    osProfile: {
      computerName: take(name, 15)
      adminUsername: adminUsername
      customData: base64(loadTextContent('./harvester-cloud-init.yaml'))
      linuxConfiguration: {
        disablePasswordAuthentication: true
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshPublicKey
            }
          ]
        }
        patchSettings: {
          patchMode: 'ImageDefault'
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: nic.id
          properties: {
            deleteOption: 'Delete'
          }
        }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

output id string = vm.id
output name string = vm.name
output dataDiskId string = dataDisk.id
