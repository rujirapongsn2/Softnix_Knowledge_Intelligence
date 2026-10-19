import React, {useCallback, useEffect, useRef, useState} from "react";
import {Button, EmptyState} from "./ui.jsx";
import {useLanguage} from "./language.jsx";
import {fieldReview} from "./metadata-states.mjs";
import {latestRequest} from "./latest-request.mjs";

const COUNTS = ["verified", "auto_verified", "unverified", "blocked", "missing"];
const display = value => value === undefined || value === null ? null : Array.isArray(value) ? value.join(", ") : String(value);

export function MetadataQualityTab({knowledgeBaseId, api, refreshKey, onOpenDocument}) {
  const {t} = useLanguage();
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const requests = useRef(latestRequest()).current;
  const load = useCallback(async () => {
    const isCurrent = requests.begin();
    setLoading(true); setError("");
    try { const next = await api(`/v1/knowledge-bases/${knowledgeBaseId}/metadata-quality`); if (isCurrent()) setReport(next); }
    catch (err) { if (isCurrent()) setError(err.message); }
    finally { if (isCurrent()) setLoading(false); }
  }, [api, knowledgeBaseId, requests]);
  useEffect(() => { setReport(null); setNotice(""); }, [knowledgeBaseId]);
  // A review in the document preview changes the numbers; reload when it saves or closes.
  useEffect(() => { load(); }, [load, refreshKey]);
  const reevaluate = async () => {
    const isCurrent = requests.begin();
    setLoading(true); setError(""); setNotice("");
    try {
      const result = await api(`/v1/knowledge-bases/${knowledgeBaseId}/metadata-reevaluate`, {method: "POST"});
      if (!isCurrent()) return;
      setNotice(t("metadataQuality.reevaluated", result));
      await load();
    } catch (err) { if (isCurrent()) { setError(err.message); setLoading(false); } }
  };
  const accuracy = row => row.ai_accuracy === null ? t("metadataQuality.accuracyNone") : `${Math.round(row.ai_accuracy * 100)}%`;
  return <section className="content-section metadata-quality">
    <div className="section-title"><div><h2>{t("metadataQuality.title")}</h2><p>{t("metadataQuality.description")}</p></div>
      <div className="page-actions"><Button label={t("metadataQuality.refresh")} size="sm" variant="ghost" isDisabled={loading} onClick={load}/><Button label={t("metadataQuality.reevaluate")} size="sm" variant="secondary" isLoading={loading && Boolean(report)} isDisabled={loading} onClick={reevaluate}/></div>
    </div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {notice && <p className="section-copy" role="status">{notice}</p>}
    {!report ? loading && <p className="section-copy" role="status">{t("documents.loading")}</p> : !report.fields.length ? <EmptyState title={t("metadataQuality.empty.title")} description={t("metadataQuality.empty.description")}/> : <>
      <div className="metadata-quality-table" role="table" aria-label={t("metadataQuality.title")}>
        <div className="metadata-quality-row metadata-quality-head" role="row"><span role="columnheader">{t("metadataQuality.column.field")}</span><span role="columnheader">{t("metadataQuality.column.accuracy")}</span>{COUNTS.map(key => <span role="columnheader" key={key}>{t(`metadataQuality.column.${key}`)}</span>)}</div>
        {report.fields.map(row => <div className="metadata-quality-row" role="row" key={`${row.template_id}:${row.field_key}`}>
          <span role="cell"><b>{row.label}</b><small>{row.template_name}</small></span>
          <span role="cell"><b>{accuracy(row)}</b><small>{t("metadataQuality.outcomes", row.outcomes)}</small></span>
          {COUNTS.map(key => <span role="cell" className={row.counts[key] && (key === "blocked" || key === "missing") ? "metadata-quality-alert" : ""} data-label={t(`metadataQuality.column.${key}`)} key={key}>{row.counts[key]}</span>)}
        </div>)}
      </div>
      <div className="section-title metadata-quality-next"><div><h3>{t("metadataQuality.nextActions.title")}</h3><p>{t("metadataQuality.nextActions.description")}</p></div>{report.next_actions_total > report.next_actions.length && <span className="section-copy">{t("metadataQuality.nextActions.more", {shown: report.next_actions.length, total: report.next_actions_total})}</span>}</div>
      {report.next_actions.length ? <div className="kb-quality-document-list">{report.next_actions.map(action => <button type="button" className={`kb-quality-document metadata-next-action metadata-field-${fieldReview(action.state)}`} key={`${action.document_id}:${action.field_key}`} onClick={() => onOpenDocument(action)}>
        <span className="kb-quality-document-main"><b>{action.title}</b><small>{action.label} · {t(`autoMetadata.state.${action.state}`)}</small></span>
        <span className="metadata-next-value">{display(action.value) ?? t("metadataQuality.noValue")}</span><span aria-hidden="true">→</span>
      </button>)}</div> : <p className="quality-all-clear">✓ {t("metadataQuality.allClear")}</p>}
    </>}
  </section>;
}
