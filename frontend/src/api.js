// Thin wrapper around the Flask API (app.py, port 5001). Centralized here
// so every call site gets the same error handling -- in particular, the
// "Flask isn't running" case, which `fetch` reports as a generic network
// error that's easy to let fail silently (a blank screen, a swallowed
// promise rejection). We turn that into a message worth reading.

const API_BASE = "http://localhost:5001";

async function apiFetch(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch {
    // fetch() throws (not a rejected-with-a-response) when the request
    // never reached a server at all -- connection refused, DNS failure,
    // CORS preflight blocked, etc. Locally, that's almost always "Flask
    // isn't running yet."
    throw new Error(
      `Can't reach the API at ${API_BASE}. Is the Flask server running? (python app.py, in the project root)`,
    );
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // No JSON body -- fine for e.g. an empty response; only a problem if
    // response.ok is also false, handled below.
  }

  if (!response.ok) {
    const message = body?.error || `Request failed (HTTP ${response.status})`;
    throw new Error(message);
  }

  return body;
}

export const api = {
  listProfiles: () => apiFetch("/api/profiles"),

  createProfile: (data) =>
    apiFetch("/api/profiles", { method: "POST", body: JSON.stringify(data) }),

  updateProfile: (id, data) =>
    apiFetch(`/api/profiles/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(data) }),

  previewDigest: (profileId) =>
    apiFetch("/api/digest/preview", {
      method: "POST",
      body: JSON.stringify({ profile_id: profileId }),
    }),

  listRuns: () => apiFetch("/api/runs"),
};
