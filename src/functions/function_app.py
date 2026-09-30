import logging

import azure.functions as func


app = func.FunctionApp()


@app.route(route="healthz", auth_level=func.AuthLevel.ANONYMOUS)
def healthz(_: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse('{"status":"ok"}', mimetype="application/json")


@app.blob_trigger(
    arg_name="intake",
    path="intake/{name}",
    connection="AzureWebJobsStorage",
    source=func.BlobSource.EVENT_GRID,
)
def inspect_intake_signature(intake: func.InputStream) -> None:
    signature = intake.read(4)
    if signature == b"\xd0\xcf\x11\xe0":
        logging.error(
            "Rejected %s: Legacy binary format detected. Open in Excel and Save As .xlsx to continue.",
            intake.name,
        )
        return
    if not signature.startswith(b"PK"):
        logging.error("Rejected %s: file is not an OpenXML package.", intake.name)
        return

    logging.info("Accepted %s for the future bounded OpenXML package-validation stage.", intake.name)
