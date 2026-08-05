import { useEffect, useState } from "react";
import { api } from "../api";

// Keywords are stored as one flat {keyword: weight} dict (see profiles.py's
// to_internal_profile()) -- there's no explicit "tier" field. A tier is
// just "every keyword that currently shares a weight value," so this
// re-derives the grouping on every render instead of tracking it as
// separate state. That also means editing a weight to match another
// tier's value naturally moves the row there on the next render, with no
// extra bookkeeping.
function groupByWeight(keywords) {
  const groups = new Map();
  for (const [kw, weight] of Object.entries(keywords)) {
    if (!groups.has(weight)) groups.set(weight, []);
    groups.get(weight).push(kw);
  }
  return [...groups.entries()]
    .map(([weight, kws]) => ({ weight, keywords: kws.sort() }))
    .sort((a, b) => b.weight - a.weight);
}

function KeywordRow({ keyword, weight, onWeightChange, onRemove }) {
  return (
    <div className="keyword-row">
      <span className="keyword-name">{keyword}</span>
      <input
        type="number"
        className="keyword-weight"
        value={weight}
        onChange={(e) => onWeightChange(Number(e.target.value))}
      />
      <button type="button" className="btn-remove" onClick={onRemove} aria-label={`Remove ${keyword}`}>
        &times;
      </button>
    </div>
  );
}

function AddKeywordForm({ defaultWeight, onAdd }) {
  const [text, setText] = useState("");
  const [weight, setWeight] = useState(defaultWeight);

  function submit(e) {
    e.preventDefault();
    const kw = text.trim();
    if (!kw) return;
    onAdd(kw, weight);
    setText("");
  }

  return (
    <form className="add-keyword-form" onSubmit={submit}>
      <input
        type="text"
        placeholder="new keyword"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <input
        type="number"
        value={weight}
        onChange={(e) => setWeight(Number(e.target.value))}
        title="weight"
      />
      <button type="submit">Add</button>
    </form>
  );
}

export default function ProfileEditor({ profiles, selectedProfileId, onSaved }) {
  const selected = profiles.find((p) => p.id === selectedProfileId);
  const [draft, setDraft] = useState(null);
  const [saveState, setSaveState] = useState(null); // null | "saving" | "saved" | "error"
  const [saveError, setSaveError] = useState(null);

  // Load a fresh editable copy whenever the selected profile changes --
  // deliberately NOT re-synced on every `profiles` change, so mid-edit
  // state survives unrelated re-renders (e.g. a background refetch).
  useEffect(() => {
    if (selected) {
      setDraft(JSON.parse(JSON.stringify(selected)));
      setSaveState(null);
      setSaveError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProfileId]);

  if (!selected || !draft) {
    return <p className="muted">No profile selected.</p>;
  }

  function updateField(field, value) {
    setDraft((d) => ({ ...d, [field]: value }));
  }

  function updateKeyword(dictField, keyword, weight) {
    setDraft((d) => ({ ...d, [dictField]: { ...d[dictField], [keyword]: weight } }));
  }

  function removeKeyword(dictField, keyword) {
    setDraft((d) => {
      const next = { ...d[dictField] };
      delete next[keyword];
      return { ...d, [dictField]: next };
    });
  }

  function addKeyword(dictField, keyword, weight) {
    setDraft((d) => {
      if (keyword in d[dictField]) return d; // no silent overwrite of an existing weight
      return { ...d, [dictField]: { ...d[dictField], [keyword]: weight } };
    });
  }

  function updateSection(index, value) {
    setDraft((d) => {
      const next = [...d.sections];
      next[index] = value;
      return { ...d, sections: next };
    });
  }

  function removeSection(index) {
    setDraft((d) => ({ ...d, sections: d.sections.filter((_, i) => i !== index) }));
  }

  function addSection() {
    setDraft((d) => ({ ...d, sections: [...d.sections, ""] }));
  }

  function updateFeed(index, field, value) {
    setDraft((d) => {
      const next = d.feeds.map((f, i) => (i === index ? { ...f, [field]: value } : f));
      return { ...d, feeds: next };
    });
  }

  function removeFeed(index) {
    setDraft((d) => ({ ...d, feeds: d.feeds.filter((_, i) => i !== index) }));
  }

  function addFeed() {
    setDraft((d) => ({ ...d, feeds: [...d.feeds, { name: "", url: "", category: "" }] }));
  }

  async function handleSave() {
    setSaveState("saving");
    setSaveError(null);
    try {
      const updated = await api.updateProfile(draft.id, draft);
      setSaveState("saved");
      onSaved(updated);
      setTimeout(() => setSaveState((s) => (s === "saved" ? null : s)), 2500);
    } catch (err) {
      setSaveState("error");
      setSaveError(err.message);
    }
  }

  const positiveTiers = groupByWeight(draft.keywords);
  const totalKeywords = Object.keys(draft.keywords).length + Object.keys(draft.negative_keywords).length;

  return (
    <div className="profile-editor">
      <section className="editor-block">
        <h2>About</h2>
        <textarea
          rows={5}
          value={draft.about}
          onChange={(e) => updateField("about", e.target.value)}
        />
      </section>

      <section className="editor-block">
        <h2>Recipient</h2>
        <input
          type="email"
          value={draft.recipient_email}
          onChange={(e) => updateField("recipient_email", e.target.value)}
        />
      </section>

      <section className="editor-block">
        <h2>Sections</h2>
        <p className="muted">Used to bucket selected stories in the digest.</p>
        {draft.sections.map((section, i) => (
          <div className="row-inline" key={i}>
            <input type="text" value={section} onChange={(e) => updateSection(i, e.target.value)} />
            <button type="button" className="btn-remove" onClick={() => removeSection(i)}>
              &times;
            </button>
          </div>
        ))}
        <button type="button" className="btn-secondary" onClick={addSection}>
          + Add section
        </button>
      </section>

      <section className="editor-block">
        <div className="editor-block-header">
          <h2>Keywords</h2>
          <span className="count-badge">{totalKeywords} total</span>
        </div>

        {positiveTiers.map(({ weight, keywords }) => (
          <div className="tier-group" key={weight}>
            <div className="tier-header">
              <strong>Weight {weight}</strong>
              <span className="count-badge">{keywords.length}</span>
            </div>
            {keywords.map((kw) => (
              <KeywordRow
                key={kw}
                keyword={kw}
                weight={draft.keywords[kw]}
                onWeightChange={(w) => updateKeyword("keywords", kw, w)}
                onRemove={() => removeKeyword("keywords", kw)}
              />
            ))}
            <AddKeywordForm defaultWeight={weight} onAdd={(kw, w) => addKeyword("keywords", kw, w)} />
          </div>
        ))}

        <div className="tier-group new-tier">
          <div className="tier-header">
            <strong>New tier</strong>
          </div>
          <AddKeywordForm defaultWeight={1} onAdd={(kw, w) => addKeyword("keywords", kw, w)} />
        </div>
      </section>

      <section className="editor-block">
        <div className="editor-block-header">
          <h2>Negative keywords</h2>
          <span className="count-badge">{Object.keys(draft.negative_keywords).length}</span>
        </div>
        <p className="muted">Actively suppress these topics rather than just deprioritize them.</p>
        {Object.keys(draft.negative_keywords)
          .sort()
          .map((kw) => (
            <KeywordRow
              key={kw}
              keyword={kw}
              weight={draft.negative_keywords[kw]}
              onWeightChange={(w) => updateKeyword("negative_keywords", kw, w)}
              onRemove={() => removeKeyword("negative_keywords", kw)}
            />
          ))}
        <AddKeywordForm defaultWeight={-3} onAdd={(kw, w) => addKeyword("negative_keywords", kw, w)} />
      </section>

      <section className="editor-block">
        <div className="editor-block-header">
          <h2>Feeds</h2>
          <span className="count-badge">{draft.feeds.length}</span>
        </div>
        {draft.feeds.map((feed, i) => (
          <div className="feed-row" key={i}>
            <input
              type="text"
              placeholder="display name"
              value={feed.name}
              onChange={(e) => updateFeed(i, "name", e.target.value)}
            />
            <input
              type="text"
              placeholder="feed URL"
              value={feed.url}
              onChange={(e) => updateFeed(i, "url", e.target.value)}
            />
            <button type="button" className="btn-remove" onClick={() => removeFeed(i)}>
              &times;
            </button>
          </div>
        ))}
        <button type="button" className="btn-secondary" onClick={addFeed}>
          + Add feed
        </button>
      </section>

      <div className="save-bar">
        <button type="button" className="btn-primary" onClick={handleSave} disabled={saveState === "saving"}>
          {saveState === "saving" ? "Saving…" : "Save"}
        </button>
        {saveState === "saved" && <span className="save-status ok">Saved</span>}
        {saveState === "error" && <span className="save-status error">Failed to save: {saveError}</span>}
      </div>
    </div>
  );
}
