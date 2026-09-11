import React, {useEffect, useState} from "react";
import {Button} from "./ui.jsx";
import {useLanguage} from "./language.jsx";

export function MetadataReviewPanel({preview, fields, api, onRefresh, MetadataFields}) {
  const {t} = useLanguage();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState({});
  const [draftRevision, setDraftRevision] = useState(0);
  const [all, setAll] = useState(false);
  useEffect(() => { setEditing(null); setDraft({}); setError(""); }, [preview.document_id]);
  const observations = preview.metadata_observations || {};
  const values = preview.document_metadata || {};
  const needsReview = field => field.fill_mode === "extract" && values[field.key] === undefined && observations[field.key]?.status !== "not_found_confirmed";
  const reviewFields = fields.filter(needsReview);
  const visible = all ? fields : reviewFields;
  const running = ["queued", "running"].includes(preview.metadata_status);
  const run = async (endpoint, body) => {
    setBusy(true); setError("");
    try {
      await api(`/v1/documents/${preview.document_id}/${endpoint}`, {method: "POST", body: JSON.stringify(body)});
      setEditing(null);
      await onRefresh();
    } catch (err) {
      setError(err.message);
      await onRefresh().catch(() => {});
    } finally { setBusy(false); }
  };
  const review = (field, action, extra = {}) => run("metadata-review", {revision: preview.metadata_revision, field_key: field.key, action, ...extra});
  return <section className="metadata-review-panel">
    <div className="preview-heading"><div><h3>{preview.metadata_template_name || t("documentPreview.metadata.defaultTitle")}</h3><p role="status">{t(`autoMetadata.${preview.metadata_status || "not_started"}`)}</p></div>
      <Button label={t("autoMetadata.fillMissing")} size="sm" variant="secondary" isDisabled={busy || running || !preview.text || !fields.length} onClick={() => run("metadata-extract", {enable_missing_fields: true})}/>
    </div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="metadata-review-switch"><button type="button" aria-pressed={!all} onClick={() => setAll(false)}>{t("autoMetadata.reviewCount", {count: reviewFields.length})}</button><button type="button" aria-pressed={all} onClick={() => setAll(true)}>{t("autoMetadata.allFields", {count: fields.length})}</button></div>
    {!visible.length && <p>{t(fields.length ? "autoMetadata.nothingToReview" : "documentPreview.metadata.noFields")}</p>}
    {visible.map(field => {
      const observation = observations[field.key] || {};
      const status = observation.status || (values[field.key] !== undefined ? "legacy_manual" : "not_started");
      return <article className="metadata-review-field" key={field.key}>
        <div className="preview-heading"><strong>{field.label}</strong><span className="metadata-status">{t(`autoMetadata.${status}`)}</span></div>
        {values[field.key] !== undefined && <p className="metadata-effective-value">{String(values[field.key])}</p>}
        {(observation.candidates || []).map((candidate, index) => <div className="metadata-candidate" key={index}>
          <div><span>{String(candidate.value)}</span>{needsReview(field) && <Button label={t("autoMetadata.confirm")} size="sm" variant="secondary" isDisabled={busy || running} onClick={() => review(field, "confirm", {candidate_index: index})}/>}</div>
          <blockquote>{candidate.evidence.quote}</blockquote>
          <details><summary>{t("autoMetadata.sourceContext")}</summary><pre>{preview.text?.slice(Math.max(0, candidate.evidence.char_start - 220), candidate.evidence.char_end + 220)}</pre></details>
        </div>)}
        {editing === field.key ? <form onSubmit={event => { event.preventDefault(); review(field, "set", {value: draft[field.key], revision: draftRevision}); }}>
          <MetadataFields fields={[{...field, required: true}]} values={draft} onChange={setDraft} isDisabled={busy}/>
          <div className="preview-actions"><Button label={t("common.save")} type="submit" size="sm" isDisabled={busy}/><Button label={t("common.cancel")} type="button" size="sm" variant="ghost" onClick={() => setEditing(null)}/></div>
        </form> : <div className="preview-actions"><Button label={t("common.edit")} size="sm" variant="ghost" isDisabled={busy} onClick={() => { setEditing(field.key); setDraftRevision(preview.metadata_revision); setDraft({[field.key]: values[field.key] ?? observation.candidates?.[0]?.value ?? ""}); }}/>{needsReview(field) && <Button label={t("autoMetadata.notFound")} size="sm" variant="ghost" isDisabled={busy || running} onClick={() => review(field, "not_found")}/>}</div>}
      </article>;
    })}
  </section>;
}
