// Monthly harvest schedule. A Consumption Logic App tags the harvester VM with a request, starts it, waits
// for the VM to power itself off, deallocates it, and fails the run unless the VM recorded success for
// that request. The VM harvests only when it boots with a pending request (src/harvester/vm_signal.py).
param name string
param location string
param tags object
param vmName string
param harvesterPrincipalId string
param workspaceId string
@minValue(1)
@maxValue(28)
param monthDay int
@minValue(0)
@maxValue(23)
param hourUtc int
param alertEmail string
@description('Earliest time the schedule may run. Without it, a new recurrence fires once immediately on deployment.')
param scheduleStart string = utcNow('yyyy-MM-ddTHH:mm:ssZ')

var armAudience = environment().resourceManager
var armBase = endsWith(armAudience, '/') ? take(armAudience, length(armAudience) - 1) : armAudience
var computeApi = '2024-07-01'
var tagsApi = '2021-04-01'
var msi = {
  type: 'ManagedServiceIdentity'
  audience: armAudience
}
var roles = {
  tagContributor: '4a9ae827-6dc8-4573-8ac7-8239d42aa03f'
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' existing = {
  name: vmName
}

var vmUrl = '${armBase}${vm.id}'
var tagsUrl = '${vmUrl}/providers/Microsoft.Resources/tags/default?api-version=${tagsApi}'
var runName = '@{workflow()?[\'run\']?[\'name\']}'
var stoppedExpression = 'or(contains(string(body(\'{0}\')?[\'statuses\']), \'PowerState/deallocated\'), contains(string(body(\'{0}\')?[\'statuses\']), \'PowerState/stopped\'))'
var resultTag = 'body(\'Read_result\')?[\'properties\']?[\'tags\']?[\'harvest-result\']'
var requestTag = 'body(\'Read_result\')?[\'properties\']?[\'tags\']?[\'harvest-request\']'
var deallocatedExpression = 'contains(string(body(\'{0}\')?[\'statuses\']), \'PowerState/deallocated\')'

resource workflow 'Microsoft.Logic/workflows@2019-05-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    state: 'Enabled'
    definition: {
      '$schema': 'https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#'
      contentVersion: '1.0.0.0'
      triggers: {
        Monthly: {
          type: 'Recurrence'
          recurrence: {
            frequency: 'Month'
            interval: 1
            startTime: scheduleStart
            timeZone: 'UTC'
            schedule: {
              monthDays: [
                monthDay
              ]
              hours: [
                hourUtc
              ]
              minutes: [
                0
              ]
            }
          }
          // One run at a time: every run uses the same VM and request tags.
          runtimeConfiguration: {
            concurrency: {
              runs: 1
            }
          }
        }
      }
      actions: {
        Get_power_state: {
          type: 'Http'
          runAfter: {}
          inputs: {
            method: 'GET'
            uri: '${vmUrl}/instanceView?api-version=${computeApi}'
            authentication: msi
          }
        }
        Require_idle_vm: {
          type: 'If'
          runAfter: {
            Get_power_state: [
              'Succeeded'
            ]
          }
          expression: '@${format(stoppedExpression, 'Get_power_state')}'
          actions: {}
          else: {
            actions: {
              VM_already_running: {
                type: 'Terminate'
                inputs: {
                  runStatus: 'Failed'
                  runError: {
                    code: 'VmAlreadyRunning'
                    message: 'The harvester VM was running, so no harvest was requested. Deallocate it and rerun this workflow.'
                  }
                }
              }
            }
          }
        }
        Request_harvest: {
          type: 'Http'
          runAfter: {
            Require_idle_vm: [
              'Succeeded'
            ]
          }
          inputs: {
            method: 'PATCH'
            uri: tagsUrl
            authentication: msi
            body: {
              operation: 'Merge'
              properties: {
                tags: {
                  'harvest-request': runName
                  'harvest-result': 'pending'
                  'harvest-request-expires': '@{addHours(utcNow(), 1, \'yyyy-MM-ddTHH:mm:ssZ\')}'
                }
              }
            }
          }
        }
        Start_vm: {
          type: 'Http'
          runAfter: {
            Request_harvest: [
              'Succeeded'
            ]
          }
          inputs: {
            method: 'POST'
            uri: '${vmUrl}/start?api-version=${computeApi}'
            authentication: msi
          }
          // Operation-status URLs are outside the VM-scoped role; poll instanceView instead.
          operationOptions: 'DisableAsyncPattern'
        }
        Wait_for_power_off: {
          type: 'Until'
          runAfter: {
            Start_vm: [
              'Succeeded'
            ]
          }
          expression: '@${format(stoppedExpression, 'Poll_power_state')}'
          limit: {
            count: 40
            timeout: 'PT6H'
          }
          actions: {
            Wait_10_minutes: {
              type: 'Wait'
              runAfter: {}
              inputs: {
                interval: {
                  count: 10
                  unit: 'Minute'
                }
              }
            }
            Poll_power_state: {
              type: 'Http'
              runAfter: {
                Wait_10_minutes: [
                  'Succeeded'
                ]
              }
              inputs: {
                method: 'GET'
                uri: '${vmUrl}/instanceView?api-version=${computeApi}'
                authentication: msi
              }
            }
            // A failed poll must not end the loop early (that would deallocate a live harvest); the
            // condition reads false and the loop polls again until the VM stops or the 6-hour limit.
            Poll_error_tolerated: {
              type: 'Compose'
              runAfter: {
                Poll_power_state: [
                  'Succeeded'
                  'Failed'
                  'TimedOut'
                ]
              }
              inputs: '@actions(\'Poll_power_state\')?[\'status\']'
            }
          }
        }
        // Once a start was attempted, always deallocate, whatever happened to the harvest. If the run never
        // reached Start_vm (power state unknown, or the request could not be written), leave the VM alone.
        Deallocate_if_started: {
          type: 'If'
          runAfter: {
            Wait_for_power_off: [
              'Succeeded'
              'Failed'
              'TimedOut'
              'Skipped'
            ]
          }
          expression: '@not(equals(actions(\'Start_vm\')?[\'status\'], \'Skipped\'))'
          actions: {
            Deallocate_vm: {
              type: 'Http'
              runAfter: {}
              inputs: {
                method: 'POST'
                uri: '${vmUrl}/deallocate?api-version=${computeApi}'
                authentication: msi
              }
              operationOptions: 'DisableAsyncPattern'
            }
            Wait_for_deallocated: {
              type: 'Until'
              runAfter: {
                Deallocate_vm: [
                  'Succeeded'
                ]
              }
              expression: '@${format(deallocatedExpression, 'Poll_deallocated')}'
              limit: {
                count: 30
                timeout: 'PT1H'
              }
              actions: {
                Wait_1_minute: {
                  type: 'Wait'
                  runAfter: {}
                  inputs: {
                    interval: {
                      count: 1
                      unit: 'Minute'
                    }
                  }
                }
                Poll_deallocated: {
                  type: 'Http'
                  runAfter: {
                    Wait_1_minute: [
                      'Succeeded'
                    ]
                  }
                  inputs: {
                    method: 'GET'
                    uri: '${vmUrl}/instanceView?api-version=${computeApi}'
                    authentication: msi
                  }
                }
              }
            }
          }
          else: {
            actions: {
              Harvest_not_started: {
                type: 'Terminate'
                inputs: {
                  runStatus: 'Failed'
                  runError: {
                    code: 'HarvestNotStarted'
                    message: 'The VM power state could not be read or the harvest request could not be written; the VM was not started. Any request tag expires on its own.'
                  }
                }
              }
            }
          }
        }
        Read_result: {
          type: 'Http'
          runAfter: {
            Deallocate_if_started: [
              'Succeeded'
              'Failed'
              'TimedOut'
            ]
          }
          inputs: {
            method: 'GET'
            uri: tagsUrl
            authentication: msi
          }
        }
        Check_result: {
          type: 'If'
          runAfter: {
            Read_result: [
              'Succeeded'
              'Failed'
              'TimedOut'
            ]
          }
          expression: '@and(equals(actions(\'Wait_for_deallocated\')?[\'status\'], \'Succeeded\'), startsWith(coalesce(${resultTag}, \'\'), concat(\'succeeded \', workflow()?[\'run\']?[\'name\'], \' \')))'
          actions: {}
          else: {
            actions: {
              // Close this run's own request if the VM never finished it (still pending or running). Only on a
              // successful tag read: a failed read must never overwrite a result the VM recorded, and an
              // unclosed pending request expires on its own (harvest-request-expires).
              Close_if_open: {
                type: 'If'
                runAfter: {}
                expression: '@and(equals(actions(\'Read_result\')?[\'status\'], \'Succeeded\'), equals(${requestTag}, workflow()?[\'run\']?[\'name\']), or(equals(${resultTag}, \'pending\'), startsWith(coalesce(${resultTag}, \'\'), \'running \')))'
                actions: {
                  Close_request: {
                    type: 'Http'
                    runAfter: {}
                    inputs: {
                      method: 'PATCH'
                      uri: tagsUrl
                      authentication: msi
                      body: {
                        operation: 'Merge'
                        properties: {
                          tags: {
                            'harvest-result': 'failed ${runName} no-result'
                          }
                        }
                      }
                    }
                  }
                }
                else: {
                  actions: {}
                }
              }
              Harvest_failed: {
                type: 'Terminate'
                runAfter: {
                  Close_if_open: [
                    'Succeeded'
                    'Failed'
                  ]
                }
                inputs: {
                  runStatus: 'Failed'
                  runError: {
                    code: 'HarvestFailed'
                    message: 'Harvest did not succeed. Result tag: @{coalesce(${resultTag}, \'unreadable\')}; deallocated: @{actions(\'Wait_for_deallocated\')?[\'status\']}. See the VM journal (journalctl -u pricing-harvest).'
                  }
                }
              }
            }
          }
        }
      }
      outputs: {}
    }
  }
}

// Only read, start, and deallocate. Virtual Machine Contributor would allow Run Command and extensions, which
// is root on the VM and so the harvester identity's staging access.
resource powerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'harvest-schedule-vm-power')
  properties: {
    roleName: 'Harvest schedule VM power (${resourceGroup().name})'
    description: 'Read, start, and deallocate the price harvester VM. No Run Command, extensions, or writes.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Compute/virtualMachines/read'
          'Microsoft.Compute/virtualMachines/instanceView/read'
          'Microsoft.Compute/virtualMachines/start/action'
          'Microsoft.Compute/virtualMachines/deallocate/action'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource vmPower 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vm.id, workflow.id, powerRole.id)
  scope: vm
  properties: {
    principalId: workflow.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: powerRole.id
  }
}

resource scheduleTags 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vm.id, workflow.id, roles.tagContributor)
  scope: vm
  properties: {
    principalId: workflow.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.tagContributor)
  }
}

// The VM records its result as a tag on itself; this is its only permission outside the staging container.
resource harvesterTags 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vm.id, harvesterPrincipalId, roles.tagContributor)
  scope: vm
  properties: {
    principalId: harvesterPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.tagContributor)
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: workflow
  properties: {
    workspaceId: workspaceId
    logs: [
      {
        category: 'WorkflowRuntime'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: 'ag-${name}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'harvest'
    enabled: true
    emailReceivers: [
      {
        name: 'harvest-owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

resource failedAlert 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${name}-failed'
  location: 'global'
  tags: tags
  properties: {
    description: 'The monthly price harvest failed or did not record success.'
    severity: 2
    enabled: true
    scopes: [
      workflow.id
    ]
    evaluationFrequency: 'PT1H'
    windowSize: 'PT1H'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'runs-failed'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'RunsFailed'
          metricNamespace: 'Microsoft.Logic/workflows'
          operator: 'GreaterThan'
          threshold: 0
          timeAggregation: 'Total'
        }
      ]
    }
    actions: empty(alertEmail) ? [] : [
      {
        actionGroupId: actionGroup.id
      }
    ]
  }
}

output name string = workflow.name
output principalId string = workflow.identity.principalId
