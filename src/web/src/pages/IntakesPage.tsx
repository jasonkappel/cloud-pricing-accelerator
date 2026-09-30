import { type DragEvent, type FormEvent, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { uploadIntake } from "../api";
import { PageHeader } from "../components/PageHeader";
import { StatusCard } from "../components/StatusCard";

export function IntakesPage() {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [selectedFileName, setSelectedFileName] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  function selectFile(file: File | undefined) {
    if (!file || !file.name.toLowerCase().endsWith(".xlsx") || file.size === 0) {
      setSelectedFile(null);
      setSelectedFileName("");
      setError("Choose an OpenXML .xlsx Intake.");
      return false;
    }
    setSelectedFile(file);
    setSelectedFileName(file.name);
    setError("");
    return true;
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (isUploading) {
      return;
    }
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
    if (event.dataTransfer.files.length !== 1) {
      setSelectedFile(null);
      setSelectedFileName("");
      setError("Drop one OpenXML .xlsx Intake.");
      return;
    }
    const file = event.dataTransfer.files[0];
    selectFile(file);
  }

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsUploading(true);
    if (!selectedFile || !selectedFile.name.toLowerCase().endsWith(".xlsx") || selectedFile.size === 0) {
      setError("Choose an OpenXML .xlsx Intake.");
      setIsUploading(false);
      return;
    }
    try {
      const detail = await uploadIntake(selectedFile);
      navigate(`/review?applicationId=${detail.application.id}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The Intake was rejected.");
    } finally {
      setIsUploading(false);
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Intake workflow"
        title="Upload a workbook"
        description="Upload the migration Intake, resolve any missing assumptions, and download the same workbook with current cloud pricing sheets added."
      />
      <div className="card-grid">
        <StatusCard title="Accepted format" value=".xlsx">
          True OpenXML packages only. OLE2, macros, external links, formulas, and unsafe ZIP packages fail
          closed.
        </StatusCard>
        <StatusCard title="Expanded package limit" value="20 MiB">
          Member count, member size, expanded size, and compression ratio are bounded before parsing.
        </StatusCard>
        <StatusCard title="Normalization" value="Cloud-neutral">
          ComputeUnit, DatabaseUnit, StorageUnit, AttributeGroups, and material Gaps are returned.
        </StatusCard>
      </div>
      <section className="panel">
        <h3>Upload an Intake workbook</h3>
        <form className="form-grid" onSubmit={handleUpload}>
          <div className="file-field">
            <label className="file-label" htmlFor="intake-file">
              Migration intake workbook
            </label>
            <div
              aria-disabled={isUploading}
              className={`file-picker${isUploading ? " disabled" : ""}`}
              onDragOver={(event) => event.preventDefault()}
              onDrop={handleDrop}
            >
              <input
                accept=".xlsx"
                className="visually-hidden"
                id="intake-file"
                name="intake"
                disabled={isUploading}
                onChange={(event) => {
                  if (!selectFile(event.currentTarget.files?.[0])) {
                    event.currentTarget.value = "";
                  }
                }}
                ref={fileInputRef}
                tabIndex={-1}
                type="file"
              />
              <button
                className="file-picker-button"
                disabled={isUploading}
                onClick={() => {
                  const input = fileInputRef.current;
                  if (input) {
                    input.value = "";
                    input.click();
                  }
                }}
                type="button"
              >
                Choose .xlsx file
              </button>
              <span aria-live="polite" className="file-picker-name">
                {selectedFileName || "or drag and drop the workbook here"}
              </span>
            </div>
          </div>
          <button disabled={isUploading} type="submit">
            {isUploading ? "Validating..." : "Upload and price"}
          </button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </section>
    </>
  );
}
