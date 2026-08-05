import { useEffect, useState } from "react";
import { api } from "../api";

function profileName(profiles, profileId) {
  if (!profileId) return "—"; // older run.log lines predate the profile field
  const match = profiles.find((p) => p.id === profileId);
  return match ? match.name : profileId; // fall back to the raw id if the profile was since deleted
}

function formatTimestamp(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export default function RunHistory({ profiles }) {
  const [state, setState] = useState("loading"); // loading | done | error
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    api
      .listRuns()
      .then((data) => {
        if (cancelled) return;
        setRuns(data);
        setState("done");
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err.message);
        setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (state === "loading") return <p className="muted">Loading run history…</p>;
  if (state === "error") return <div className="error-banner">Could not load run history: {error}</div>;
  if (runs.length === 0) return <p className="muted">No runs logged yet.</p>;

  return (
    <table className="run-table">
      <thead>
        <tr>
          <th>Date</th>
          <th>Profile</th>
          <th>Items</th>
          <th>Cost</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run, i) => (
          <tr key={i} className={`status-${run.status ?? "unknown"}`}>
            <td>{formatTimestamp(run.timestamp)}</td>
            <td>{profileName(profiles, run.profile)}</td>
            <td>{run.items ?? "—"}</td>
            <td>{run.cost ?? "—"}</td>
            <td>
              <span className="status-pill">{run.status ?? "unknown"}</span>
              {run.detail && <span className="run-detail" title={run.detail}> ({run.detail})</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
