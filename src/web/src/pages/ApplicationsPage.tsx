import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { listApplications, type Application } from "../api";
import { PageHeader } from "../components/PageHeader";

export function ApplicationsPage() {
  const [applications, setApplications] = useState<Application[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    listApplications().then(setApplications).catch((requestError: unknown) => {
      setError(requestError instanceof Error ? requestError.message : "The API is unavailable.");
    });
  }, []);

  return (
    <>
      <PageHeader
        eyebrow="Application registry"
        title="Applications"
        description="Each accepted Intake creates one workload-isolated Application."
      />
      {error && <p className="error" role="alert">{error}</p>}
      <section className="panel">
        <div className="section-heading">
          <h3>Registered Applications</h3>
          <span>{applications.length}</span>
        </div>
        {applications.length === 0 ? (
          <p className="empty-state">
            No Applications are registered. Upload the supplied synthetic Intake to begin.
          </p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Application</th>
                  <th>Intake</th>
                  <th>Comparison state</th>
                  <th>Review</th>
                </tr>
              </thead>
              <tbody>
                {applications.map((application) => (
                  <tr key={application.id}>
                    <td>{application.name}</td>
                    <td>{application.intake_file_name}</td>
                    <td><span className="status">{application.comparison_state}</span></td>
                    <td>
                      <Link to={`/review?applicationId=${application.id}`}>Open review</Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
