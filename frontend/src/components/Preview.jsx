import { useEffect, useRef, useState } from "react";
import { api } from "../api";

function ElapsedTimer() {
  // The preview call takes 30-60s (fetch ~800 items across 21 feeds, then
  // one Claude call). A static "Loading..." over that long a wait reads as
  // frozen/broken -- a running clock is proof it's still working.
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return <span>{seconds}s</span>;
}

function ItemCard({ item }) {
  return (
    <article className="preview-item">
      <div className="preview-item-header">
        <span className="section-badge">{item.section}</span>
        <span className="theme-badge" title="Theme (used internally for diversity capping)">
          {item.theme}
        </span>
      </div>
      <h3>
        <a href={item.link} target="_blank" rel="noreferrer">
          {item.headline}
        </a>
      </h3>
      <p className="summary">{item.two_sentence_summary}</p>
      <p className="why-it-matters">{item.why_it_matters}</p>
      <p className="source-line">
        Source: <a href={item.link} target="_blank" rel="noreferrer">{item.source}</a>
      </p>

      {/* Score + matched keywords: secondary to the content above, but
          never hidden -- this is the whole reason this view exists. */}
      <div className="diagnostics">
        <span className="score-badge">score {item.score}</span>
        <div className="matched-keywords">
          {item.matched_keywords.length === 0 ? (
            <span className="muted">no keyword matches recorded</span>
          ) : (
            item.matched_keywords.map((m, i) => (
              <span className="keyword-tag" key={i}>
                {m}
              </span>
            ))
          )}
        </div>
      </div>
    </article>
  );
}

export default function Preview({ profileId }) {
  const [state, setState] = useState("idle"); // idle | loading | done | error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const requestId = useRef(0);

  async function runPreview() {
    const thisRequest = ++requestId.current;
    setState("loading");
    setError(null);
    try {
      const data = await api.previewDigest(profileId);
      if (requestId.current !== thisRequest) return; // a newer request superseded this one
      setResult(data);
      setState("done");
    } catch (err) {
      if (requestId.current !== thisRequest) return;
      setError(err.message);
      setState("error");
    }
  }

  return (
    <div className="preview-view">
      <div className="preview-controls">
        <button type="button" className="btn-primary" onClick={runPreview} disabled={state === "loading" || !profileId}>
          {state === "loading" ? "Running…" : "Run preview"}
        </button>
        {state === "loading" && (
          <span className="loading-indicator">
            Fetching feeds, filtering, and asking Claude to curate -- usually 30-60s (<ElapsedTimer />)
          </span>
        )}
      </div>

      {state === "error" && <div className="error-banner">Preview failed: {error}</div>}

      {state === "done" && result && (
        <>
          <div className="run-stats">
            <div className="stat">
              <span className="stat-value">{result.raw_count}</span>
              <span className="stat-label">raw items fetched</span>
            </div>
            <div className="stat">
              <span className="stat-value">{result.candidate_count}</span>
              <span className="stat-label">candidates after filtering</span>
            </div>
            <div className="stat">
              <span className="stat-value">{result.items.length}</span>
              <span className="stat-label">selected</span>
            </div>
            <div className="stat">
              <span className="stat-value">${result.usage?.estimated_cost_usd?.toFixed(4) ?? "?"}</span>
              <span className="stat-label">estimated cost</span>
            </div>
          </div>

          <div className="preview-items">
            {result.items.map((item, i) => (
              <ItemCard item={item} key={item.link ?? i} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
