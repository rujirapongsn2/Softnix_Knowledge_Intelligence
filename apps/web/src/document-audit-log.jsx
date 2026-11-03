import React, {useEffect, useRef, useState} from "react";
import {Badge, Button, Card, EmptyState, Selector, TextInput} from "./ui.jsx";
import {useLanguage} from "./language.jsx";
import "./document-audit-log.css";

const emptyFilters = {search: "", knowledge_base_id: "", action: "", actor: "", channel: "", from_ts: "", to_ts: "", document_id: ""};
const emptyPage = {items: [], total: 0, next_cursor: null, filter_options: {knowledge_bases: [], actors: [], actions: []}};

export function DocumentAuditLog({api, onOpenDocument}) {
  const {t, language} = useLanguage();
  const [draft, setDraft] = useState(emptyFilters);
  const [applied, setApplied] = useState({});
  const [page, setPage] = useState(emptyPage);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const sequence = useRef(0);
  const actionLabel = action => {
    const key = `audit.action.${action}`;
    const label = t(key);
    return label === key ? action : label;
  };
  const update = (key, value) => setDraft(current => ({...current, [key]: value}));
  const load = async (filters, append = false, cursor = null, clear = false) => {
    const request = ++sequence.current;
    setLoading(true);
    setError("");
    if (clear) { setPage(current => ({...current, items: [], total: 0, next_cursor: null})); setExpanded(null); }
    const query = new URLSearchParams({...filters, limit: "50"});
    if (cursor) query.set("cursor", cursor);
    try {
      const result = await api(`/v1/logs/audit?${query}`);
      if (request !== sequence.current) return;
      setPage(current => ({...result, items: append ? [...new Map([...current.items, ...result.items].map(item => [item.id, item])).values()] : result.items}));
      setStale(false);
    } catch (failure) {
      if (request !== sequence.current) return;
      setError(failure.message || t("audit.loadError"));
      setStale(!clear);
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  };
  useEffect(() => {
    load({});
    return () => { sequence.current += 1; };
  }, [api]);
  const apply = event => {
    event.preventDefault();
    const filters = Object.fromEntries(Object.entries(draft).filter(([, value]) => value).map(([key, value]) => [key, value.trim()]));
    const start = filters.from_ts ? new Date(filters.from_ts) : null;
    const end = filters.to_ts ? new Date(filters.to_ts) : null;
    if (start && !Number.isFinite(start.getTime()) || end && !Number.isFinite(end.getTime())) { setError(t("audit.invalidDate")); return; }
    if (start && end && start > end) { setError(t("audit.invalidRange")); return; }
    if (start) filters.from_ts = start.toISOString();
    if (end) filters.to_ts = end.toISOString();
    if (filters.actor) {
      const separator = filters.actor.indexOf(":");
      filters.actor_type = filters.actor.slice(0, separator);
      filters.actor_id = filters.actor.slice(separator + 1);
      delete filters.actor;
    }
    setApplied(filters);
    load(filters, false, null, true);
  };
  const reset = () => { setDraft(emptyFilters); setApplied({}); load({}, false, null, true); };
  const options = page.filter_options;
  const all = [{value: "", label: t("audit.all")}];
  return <section className="document-audit" id="document-audit-panel" role="tabpanel" aria-labelledby="document-audit-tab">
    <Card padding={4}>
      <form onSubmit={apply} className="audit-filters">
        <div className="audit-filter-primary">
          <TextInput label={t("audit.search")} value={draft.search} onChange={value => update("search", value)} maxLength={200}/>
          <Selector label={t("audit.kb")} value={draft.knowledge_base_id} onChange={value => update("knowledge_base_id", value)} options={[...all, ...options.knowledge_bases.map(kb => ({value: kb.id, label: kb.name || kb.id}))]}/>
          <Selector label={t("audit.event")} value={draft.action} onChange={value => update("action", value)} options={[...all, ...options.actions.map(action => ({value: action, label: actionLabel(action)}))]}/>
        </div>
        <details className="audit-more-filters">
          <summary>{t("audit.moreFilters")}{[draft.actor, draft.channel, draft.from_ts, draft.to_ts, draft.document_id].filter(Boolean).length > 0 && <Badge label={[draft.actor, draft.channel, draft.from_ts, draft.to_ts, draft.document_id].filter(Boolean).length}/>}</summary>
          <div className="audit-filter-secondary">
            <Selector label={t("audit.actor")} value={draft.actor} onChange={value => update("actor", value)} options={[...all, ...options.actors.map(actor => ({value: `${actor.type}:${actor.id}`, label: `${actor.name} · ${t(`audit.actorType.${actor.type}`)}`}))]}/>
            <Selector label={t("audit.channel")} value={draft.channel} onChange={value => update("channel", value)} options={[...all, ...["gui", "ingest_api", "system"].map(value => ({value, label: t(`audit.channel.${value}`)}))]}/>
            <TextInput label={t("audit.from")} type="datetime-local" value={draft.from_ts} onChange={value => update("from_ts", value)}/>
            <TextInput label={t("audit.to")} type="datetime-local" value={draft.to_ts} onChange={value => update("to_ts", value)}/>
            <TextInput label={t("audit.documentId")} value={draft.document_id} onChange={value => update("document_id", value)} maxLength={100}/>
          </div>
        </details>
        <div className="audit-filter-actions">
          <Button label={t("audit.apply")} variant="primary" type="submit" isLoading={loading}/>
          <Button label={t("audit.reset")} variant="ghost" type="button" onClick={reset}/>
          <Button label={t("audit.refresh")} variant="secondary" type="button" onClick={() => load(applied)} isDisabled={loading}/>
        </div>
      </form>
    </Card>
    {error && <p className="inline-error" role="alert">{error}{stale && page.items.length ? ` ${t("audit.stale")}` : ""}</p>}
    <div className="audit-result-heading" role="status" aria-live="polite"><span>{loading ? t("audit.loading") : t("audit.results", {shown: page.items.length, total: page.total})}</span></div>
    <div aria-busy={loading} className="audit-events">
      {page.items.map(item => <article className="audit-event" key={item.id}>
        <button type="button" className="audit-event-summary" aria-expanded={expanded === item.id} aria-controls={`audit-${item.id}`} onClick={() => setExpanded(expanded === item.id ? null : item.id)}>
          <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString(language === "th" ? "th-TH" : "en-GB")}</time>
          <span className="audit-event-action"><b>{actionLabel(item.action)}</b></span>
          <span className="audit-event-document"><b>{item.document?.title || item.document?.filename || item.details.filename || t("audit.noDocument")}</b><small>{item.knowledge_base?.name || t("audit.unknownKb")}{item.document?.deleted ? ` · ${t("audit.deleted")}` : ""}</small></span>
          <span className="audit-event-actor"><b>{item.actor.type === "system" ? t("audit.actorType.system") : item.actor.name}</b><small>{t(`audit.channel.${item.channel}`)}</small></span>
          <span aria-hidden="true" className="audit-expand">{expanded === item.id ? "−" : "+"}</span>
        </button>
        {expanded === item.id && <div id={`audit-${item.id}`} className="audit-event-detail">
          <dl>
            <div><dt>{t("audit.eventId")}</dt><dd><code>{item.id}</code></dd></div>
            <div><dt>{t("audit.event")}</dt><dd><code>{item.action}</code></dd></div>
            {item.document && <div><dt>{t("audit.documentId")}</dt><dd><code>{item.document.id}</code></dd></div>}
            {item.knowledge_base && <div><dt>{t("audit.kbId")}</dt><dd><code>{item.knowledge_base.id}</code></dd></div>}
            {Object.entries(item.details).map(([key, value]) => <div key={key}><dt>{t(`audit.detail.${key}`) === `audit.detail.${key}` ? key : t(`audit.detail.${key}`)}</dt><dd>{Array.isArray(value) ? value.join(", ") : value === null ? "—" : typeof value === "boolean" ? t(value ? "audit.yes" : "audit.no") : String(value)}</dd></div>)}
          </dl>
          {item.document?.available && onOpenDocument && <Button label={t("audit.openDocument")} size="sm" onClick={() => onOpenDocument(item)}/>}
        </div>}
      </article>)}
      {!loading && !error && !page.items.length && <EmptyState isCompact title={t("audit.empty")} description={t("audit.emptyHelp")}/>}
    </div>
    {page.next_cursor && <div className="log-load-more"><Button label={t("audit.loadMore")} onClick={() => load(applied, true, page.next_cursor)} isLoading={loading}/></div>}
  </section>;
}
