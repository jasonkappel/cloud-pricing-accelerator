# Intake Function shell

Azure Functions Python v2 shell for the intake path. It exposes anonymous `/api/healthz` and watches
the private `intake` container. The blob trigger rejects OLE2 and non-ZIP signatures before the bounded
OpenXML package validator is implemented.

The Function deployment is intentionally locked off in `infra/main.bicep`. Before removing that gate:

- Add private endpoints and private DNS for the Storage blob, queue, and table services.
- Add Flex Consumption VNet integration so deployment storage and `AzureWebJobsStorage` are reachable.
- Add an Event Grid system topic and a `Microsoft.Storage.BlobCreated` subscription filtered to the
  `intake` container and routed to the Event Grid blob trigger.
