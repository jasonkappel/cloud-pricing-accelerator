# src

| Folder | What it is |
| --- | --- |
| `api/` | FastAPI pricing API: OpenXML intake validation, cloud-neutral normalization, typed Gap resolution, the deterministic `decimal` pricing engine with the BOM-completeness check and run hash, the CompletenessGate, and the priced workbook and evidence exports. See `api/README.md`. |
| `web/` | React review web app: the guided flow (Mode, Upload, Resolve, Answer, Export), Home, Estimates, Price book, Settings, and the Builder detail pages. It formats API numbers and never computes them. See `web/README.md`. |
| `harvester/` | Streaming collectors for the Azure Retail Prices API and the AWS Price List bulk feed, validation, reconciliation, and the staged snapshot and approval path. See `harvester/README.md`. |
| `functions/` | Blob-triggered intake Function shell. Not deployed until private networking is added. |

## The one rule that governs this folder

No price, percentage, ranking, or delta may originate anywhere except the API's pricing engine. Every number
is computed there with `decimal` and stamped with a run hash. The web app, and any future Foundry agent,
only quote what the engine already produced. If you find a code path where anything else produces a number
the user sees, that is the bug.
