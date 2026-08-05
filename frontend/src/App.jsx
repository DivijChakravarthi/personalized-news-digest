import { useEffect, useState } from "react";
import "./App.css";
import { api } from "./api";
import ProfileEditor from "./components/ProfileEditor";
import Preview from "./components/Preview";
import RunHistory from "./components/RunHistory";

const TABS = ["Profile", "Preview", "History"];

export default function App() {
  const [tab, setTab] = useState(TABS[0]);
  const [profiles, setProfiles] = useState([]);
  const [selectedProfileId, setSelectedProfileId] = useState(null);
  const [loadState, setLoadState] = useState("loading"); // loading | done | error
  const [loadError, setLoadError] = useState(null);

  useEffect(() => {
    api
      .listProfiles()
      .then((data) => {
        setProfiles(data);
        if (data.length > 0) setSelectedProfileId(data[0].id);
        setLoadState("done");
      })
      .catch((err) => {
        setLoadError(err.message);
        setLoadState("error");
      });
  }, []);

  // Called by ProfileEditor after a successful save -- keeps the shared
  // profiles list (used by the selector and by RunHistory's name lookup)
  // in sync without a full refetch.
  function handleProfileSaved(updated) {
    setProfiles((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>news-digest</h1>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t}
              className={t === tab ? "tab active" : "tab"}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
        {profiles.length > 0 && (
          <select
            className="profile-select"
            value={selectedProfileId ?? ""}
            onChange={(e) => setSelectedProfileId(e.target.value)}
          >
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </header>

      <main className="app-main">
        {loadState === "error" && (
          <div className="error-banner">Could not load profiles: {loadError}</div>
        )}
        {loadState === "loading" && <p className="muted">Loading profiles…</p>}

        {loadState === "done" && profiles.length === 0 && (
          <p className="muted">
            No profiles found. Copy profiles.example.json to profiles.json in the project root
            and restart the Flask server.
          </p>
        )}

        {loadState === "done" && profiles.length > 0 && (
          <>
            {tab === "Profile" && (
              <ProfileEditor
                profiles={profiles}
                selectedProfileId={selectedProfileId}
                onSaved={handleProfileSaved}
              />
            )}
            {tab === "Preview" && <Preview profileId={selectedProfileId} />}
            {tab === "History" && <RunHistory profiles={profiles} />}
          </>
        )}
      </main>
    </div>
  );
}
