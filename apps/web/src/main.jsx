import React, {useCallback, useEffect, useMemo, useState} from "react";
import {createRoot} from "react-dom/client";
import {Theme} from "@astryxdesign/core/theme";
import {neutralTheme} from "@astryxdesign/theme-neutral/built";
import {AppShell} from "@astryxdesign/core/AppShell";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {CheckboxInput} from "@astryxdesign/core/CheckboxInput";
import {EmptyState} from "@astryxdesign/core/EmptyState";
import {FileInput} from "@astryxdesign/core/FileInput";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {SideNav, SideNavHeading, SideNavItem, SideNavSection} from "@astryxdesign/core/SideNav";
import {Selector} from "@astryxdesign/core/Selector";
import {TextArea} from "@astryxdesign/core/TextArea";
import {TextInput} from "@astryxdesign/core/TextInput";
import {Toast} from "@astryxdesign/core/Toast";
import {TopNav, TopNavHeading} from "@astryxdesign/core/TopNav";
import {Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, useEdgesState, useNodesState, useReactFlow} from "@xyflow/react";
import "@astryxdesign/core/reset.css";
import "@astryxdesign/core/astryx.css";
import "@astryxdesign/theme-neutral/theme.css";
import "@xyflow/react/dist/style.css";
import "./style.css";
import "./access.css";
import "./branding.css";
import "./graph-visual.css";
import "./graph-topology.css";
import "./retrieval-policy.css";
import "./legal-registry.css";
import "./knowledge-hub.css";
import "./logging.css";
import {connectionHandles} from "./graph-geometry.mjs";

const ACCEPTED_FILES = ".pdf,.docx,.pptx,.xlsx,.xls,.txt,.md,.html,.htm,.csv,.json";
const MAX_FILE_SIZE = 50 * 1024 * 1024;
const DOCUMENT_TYPE_OPTIONS = [
  {value: "general", label: "General document", description: "Search, citations, and knowledge graph"},
  {value: "legal", label: "Legal document", description: "Automatically extracts legal metadata"},
  {value: "regulation", label: "Regulation / policy", description: "Automatically extracts clauses and amendments"},
  {value: "contract", label: "Contract", description: "Automatically extracts parties, obligations, and terms"},
];
const documentTypeLabel = type => DOCUMENT_TYPE_OPTIONS.find(option => option.value === type)?.label || "General document";

// Keep native HTML semantics where the browser owns the interaction (file/date
// inputs), but route all choice fields through Astryx Selector. This gives the
// product one accessible, keyboard-friendly control with the active Meta theme
// instead of a mix of browser-specific <select> rendering and Astryx controls.
const DesignSystemSelect = ({label, value, onChange, options, isLabelHidden = false, isDisabled = false, className, size = "sm", description}) => (
  <Selector
    label={label}
    isLabelHidden={isLabelHidden}
    value={value}
    onChange={onChange}
    options={options}
    isDisabled={isDisabled}
    className={className}
    size={size}
    description={description}
  />
);
const DesignSystemCheckbox = ({label, checked, onChange, isDisabled = false, className}) => (
  <CheckboxInput label={label} value={checked} onChange={onChange} isDisabled={isDisabled} className={className} size="sm"/>
);

let refreshSessionRequest = null;

const api = async (path, init = {}, mayRefresh = true) => {
  const headers = {"Content-Type": "application/json", ...(init.headers || {})};
  if (init.body instanceof FormData) delete headers["Content-Type"];
  const response = await fetch(`/api${path}`, {credentials: "include", headers, ...init});
  if (response.status === 401 && mayRefresh && !path.startsWith("/v1/auth/")) {
    refreshSessionRequest ||= fetch("/api/v1/auth/refresh", {method: "POST", credentials: "include"}).finally(() => { refreshSessionRequest = null; });
    const refreshed = await refreshSessionRequest;
    if (refreshed.ok) return api(path, init, false);
    window.dispatchEvent(new Event("softnix:session-expired"));
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || data.detail || "Request failed");
  return data;
};

function Login({onLogin}) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const submit = async event => {
    event.preventDefault(); setError(""); setIsLoading(true);
    try { onLogin(await api("/v1/auth/login", {method: "POST", body: JSON.stringify({username, password})})); }
    catch (reason) { setError(reason.message); }
    finally { setIsLoading(false); }
  };
  return <main className="login-page"><section className="login-card"><img className="login-logo" src="/logo-softnix.png" alt="Softnix"/>
    <p className="eyebrow">SOFTNIX · KNOWLEDGE INTELLIGENCE</p><h1>Make knowledge useful.</h1>
    <p className="login-copy">Sign in to organize trusted documents, explore relationships, and get cited answers.</p>
    <form className="form-stack" onSubmit={submit}>
      <TextInput label="Username" value={username} onChange={setUsername} isRequired hasAutoFocus/>
      <TextInput label="Password" type="password" value={password} onChange={setPassword} isRequired/>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <Button label="Sign in to workspace" type="submit" variant="primary" size="lg" isLoading={isLoading}/>
    </form>
  </section></main>;
}

function App() {
  const [user, setUser] = useState(null);
  const [isSessionLoading, setIsSessionLoading] = useState(true);
  const [kbs, setKbs] = useState([]);
  const [selectedKbId, setSelectedKbId] = useState("");
  const [entities, setEntities] = useState([]);
  const [relationships, setRelationships] = useState([]);
  const [documents, setDocuments] = useState([]);
  const [documentPreview, setDocumentPreview] = useState(null);
  const [documentJobs, setDocumentJobs] = useState([]);
  const [graph, setGraph] = useState(null);
  const [impact, setImpact] = useState(null);
  const [query, setQuery] = useState("");
  const [queryAsOfDate, setQueryAsOfDate] = useState("");
  const [queryIncludeHistorical, setQueryIncludeHistorical] = useState(false);
  const [queryResult, setQueryResult] = useState(null);
  const [isQuerying, setIsQuerying] = useState(false);
  const [message, setMessage] = useState(null);
  const [activeView, setActiveView] = useState("knowledge-bases");
  const [newKbName, setNewKbName] = useState("");
  const [uploadFile, setUploadFile] = useState([]);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadDocumentType, setUploadDocumentType] = useState("general");
  const [isUploading, setIsUploading] = useState(false);
  const [showDeletedDocuments, setShowDeletedDocuments] = useState(false);
  const [tokens, setTokens] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
  const [mcpActivity, setMcpActivity] = useState([]);
  const [transactionLogs, setTransactionLogs] = useState([]);
  const [traceLogs, setTraceLogs] = useState([]);
  const [transactionCursor, setTransactionCursor] = useState(null);
  const [traceCursor, setTraceCursor] = useState(null);
  const [legalGraphView, setLegalGraphView] = useState("verified");
  const [isLegalGraph, setIsLegalGraph] = useState(false);
  const [legalRebuildStatus, setLegalRebuildStatus] = useState(null);
  const [legalInstruments, setLegalInstruments] = useState([]);
  const selectedKb = useMemo(() => kbs.find(kb => kb.id === selectedKbId), [kbs, selectedKbId]);
  const completedDocuments = documents.filter(document => document.status === "completed").length;
  const processingDocuments = documents.filter(document => ["queued", "extracting", "indexing"].includes(document.status) || ["queued", "running"].includes(document.processing_job_status)).length;

  const notify = (body, type = "info") => setMessage({body, type, id: Date.now()});
  const showError = error => notify(error.message || "Something went wrong. Please try again.", "error");
  useEffect(() => {
    let active = true;
    const expireSession = () => { setUser(null); setKbs([]); setSelectedKbId(""); };
    window.addEventListener("softnix:session-expired", expireSession);
    api("/v1/auth/me").catch(async () => {
      const refreshed = await fetch("/api/v1/auth/refresh", {method: "POST", credentials: "include"});
      if (!refreshed.ok) throw new Error("Session expired");
      return api("/v1/auth/me", {}, false);
    }).then(session => { if (active) setUser(session); }).catch(() => undefined).finally(() => { if (active) setIsSessionLoading(false); });
    return () => { active = false; window.removeEventListener("softnix:session-expired", expireSession); };
  }, []);
  const loadKbs = async () => {
    const rows = await api("/v1/knowledge-bases"); setKbs(rows);
    setSelectedKbId(current => rows.some(kb => kb.id === current) ? current : rows[0]?.id || "");
  };
  const loadKbData = async (id, includeDeleted = showDeletedDocuments) => {
    if (!id) { setEntities([]); setRelationships([]); setDocuments([]); setIsLegalGraph(false); setLegalInstruments([]); return; }
    const nextDocuments = await api(`/v1/knowledge-bases/${id}/documents${includeDeleted ? "?include_deleted=true" : ""}`);
    const hasLegalDocuments = nextDocuments.some(document => ["legal", "regulation", "contract"].includes(document.document_type));
    const graphData = hasLegalDocuments
      ? await api(`/v1/knowledge-bases/${id}/legal-graph?view=${legalGraphView}`)
      : await Promise.all([api(`/v1/knowledge-bases/${id}/entities`), api(`/v1/knowledge-bases/${id}/relationships`)]);
    const [nextEntities, nextRelationships] = hasLegalDocuments ? [graphData.nodes, graphData.edges] : graphData;
    setEntities(nextEntities); setRelationships(nextRelationships); setDocuments(nextDocuments);
    setIsLegalGraph(hasLegalDocuments);
    setLegalInstruments(hasLegalDocuments ? await api(`/v1/knowledge-bases/${id}/legal-registry`) : []);
    setGraph(null); setImpact(null); setDocumentPreview(null); setDocumentJobs([]);
  };
  useEffect(() => { if (user) loadKbs().catch(showError); }, [user]);
  useEffect(() => { setLegalRebuildStatus(null); }, [selectedKbId]);
  useEffect(() => { if (user) loadKbData(selectedKbId).catch(showError); }, [selectedKbId, user, showDeletedDocuments, legalGraphView]);
  useEffect(() => {
    if (!user || activeView !== "documents" || !selectedKbId || processingDocuments === 0) return undefined;
    const timer = window.setInterval(() => loadKbData(selectedKbId).catch(showError), 5000);
    return () => window.clearInterval(timer);
  }, [activeView, selectedKbId, user, processingDocuments, showDeletedDocuments]);
  useEffect(() => {
    if (!user || !selectedKbId || !legalRebuildStatus || !["queued", "running"].includes(legalRebuildStatus.status)) return undefined;
    const poll = async () => {
      try {
        const status = await api(`/v1/knowledge-bases/${selectedKbId}/legal-graph/rebuild`);
        setLegalRebuildStatus(status);
        if (status.status === "completed") { await loadKbData(selectedKbId); notify("Legal graph rebuild completed."); }
      } catch (error) { showError(error); }
    };
    const timer = window.setInterval(poll, 2500); poll();
    return () => window.clearInterval(timer);
  }, [user, selectedKbId, legalRebuildStatus?.status]);

  const createKb = async event => {
    event.preventDefault(); const name = newKbName.trim(); if (!name) return;
    try {
      // The API owns code generation so Thai/non-Latin names and soft-deleted
      // codes cannot collapse into the same fallback slug.
      const kb = await api("/v1/knowledge-bases", {method: "POST", body: JSON.stringify({name})});
      setKbs(items => [...items, kb]); setSelectedKbId(kb.id); setNewKbName(""); setActiveView("documents"); notify("Knowledge Base created. Upload your first document to begin.");
    } catch (error) { showError(error); }
  };
  const manageKnowledgeBase = async (knowledgeBase, action) => {
    if (action === "delete" && !window.confirm(`Delete ${knowledgeBase.name}? This is only available after all documents are removed.`)) return;
    try {
      await api(`/v1/knowledge-bases/${knowledgeBase.id}${action === "delete" ? "" : `/${action}`}`, {method: action === "delete" ? "DELETE" : "POST"});
      await loadKbs();
      notify(`Knowledge Base ${action === "delete" ? "deleted" : action === "disable" ? "disabled" : "activated"}.`);
    } catch (error) { showError(error); }
  };
  const updateRetrievalConfig = async (knowledgeBase, config) => {
    try {
      const updated = await api(`/v1/knowledge-bases/${knowledgeBase.id}/retrieval-config`, {method: "PATCH", body: JSON.stringify(config)});
      setKbs(items => items.map(item => item.id === updated.id ? updated : item));
      notify("Retrieval policy updated for this Knowledge Base.");
    } catch (error) { showError(error); }
  };
  const addEntity = async ({name, entityType}) => {
    if (!selectedKbId || !name?.trim()) return null;
    try {
      const entity = await api(`/v1/knowledge-bases/${selectedKbId}/entities`, {method: "POST", body: JSON.stringify({name: name.trim(), entity_type: entityType || "Application"})});
      setEntities(items => [...items, entity]); notify("Entity added to the graph."); return entity;
    } catch (error) { showError(error); }
  };
  const addRelationship = async ({sourceEntityId, targetEntityId, relationshipType}) => {
    if (!selectedKbId || !sourceEntityId || !targetEntityId || sourceEntityId === targetEntityId) return null;
    try {
      const relationship = await api(`/v1/knowledge-bases/${selectedKbId}/relationships`, {method: "POST", body: JSON.stringify({source_entity_id: sourceEntityId, target_entity_id: targetEntityId, relationship_type: relationshipType || "DEPENDS_ON"})});
      setRelationships(items => [...items, relationship]); notify("Relationship added to the graph."); return relationship;
    } catch (error) { showError(error); }
  };
  const syncGraphFromDocuments = async () => {
    if (!selectedKbId) return;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/graph/sync`, {method: "POST"});
      await loadKbData(selectedKbId);
      notify(`Graph sync completed: ${result.entities} new entities and ${result.relationships} new relationships.`);
      return result;
    } catch (error) { showError(error); return null; }
  };
  const queueLegalGraphRebuild = async () => {
    if (!selectedKbId) return null;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/legal-graph/rebuild`, {method: "POST"});
      setLegalRebuildStatus({status: result.status, progress_percent: 0});
      notify("Legal graph rebuild queued. The verified structure will refresh when it completes.");
      return result;
    } catch (error) { showError(error); return null; }
  };
  const reviewLegalRelationship = async (relationshipId, status) => {
    try {
      await api(`/v1/relationships/${relationshipId}/legal-review`, {method: "PATCH", body: JSON.stringify({status})});
      await loadKbData(selectedKbId); notify(status === "verified" ? "Legal relationship approved." : "Legal relationship rejected.");
    } catch (error) { showError(error); }
  };
  const resolveLegalRegistry = async () => {
    if (!selectedKbId) return;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/legal-registry/resolve`, {method: "POST"});
      await loadKbData(selectedKbId); notify(`Legal registry resolved: ${result.changed} status change(s) across ${result.instruments} instrument(s).`);
    } catch (error) { showError(error); }
  };
  const updateLegalInstrument = async (instrumentId, payload) => {
    try { await api(`/v1/legal-instruments/${instrumentId}`, {method: "PATCH", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify("Legal instrument updated."); }
    catch (error) { showError(error); }
  };
  const runQuery = async event => {
    event.preventDefault(); if (!query.trim() || isQuerying) return;
    setIsQuerying(true);
    try {
      setQueryResult(await api("/v1/query", {method: "POST", body: JSON.stringify({
        query, knowledge_base_ids: selectedKbId ? [selectedKbId] : [],
        filters: {as_of_date: queryAsOfDate || null, include_historical: queryIncludeHistorical},
      })}));
    }
    catch (error) { showError(error); }
    finally { setIsQuerying(false); }
  };
  const analyzeImpact = async ({subject, scenario, entityId}) => {
    if (!selectedKbId || !subject?.trim() || !scenario?.trim()) return;
    try { setImpact(await api("/v1/query/impact", {method: "POST", body: JSON.stringify({subject: subject.trim(), entity_id: entityId || null, scenario: scenario.trim(), knowledge_base_ids: [selectedKbId], max_depth: 3})})); }
    catch (error) { showError(error); }
  };
  const uploadDocument = async event => {
    event.preventDefault(); if (!selectedKbId || !uploadFile.length) return;
    const form = new FormData(); uploadFile.forEach(file => form.append("files", file)); form.append("document_type", uploadDocumentType); if (uploadFile.length === 1 && uploadTitle.trim()) form.append("title", uploadTitle.trim());
    setIsUploading(true);
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/documents/batch`, {method: "POST", body: form});
      const selectedCount = uploadFile.length;
      setUploadFile([]); setUploadTitle(""); setUploadDocumentType("general"); await loadKbData(selectedKbId);
      notify(result.failed_count ? `${result.queued_count}/${selectedCount} files queued. ${result.failed_count} failed — retry them from the document list.` : `${result.queued_count} files queued. Processing runs independently for each file.`);
    } catch (error) { showError(error); }
    finally { setIsUploading(false); }
  };
  const extractLegalMetadata = async document => {
    try { await api(`/v1/documents/${document.id}/legal-extract`, {method: "POST"}); await openDocument(document); notify("Legal metadata extraction queued. Review the result when processing completes."); }
    catch (error) { showError(error); }
  };
  const saveLegalMetadata = async (document, metadata) => {
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "PUT", body: JSON.stringify({metadata})}); await openDocument(document); await queueLegalGraphRebuild(); notify("Legal metadata saved. A legal graph rebuild has been queued."); }
    catch (error) { showError(error); throw error; }
  };
  const deleteLegalMetadata = async document => {
    if (!window.confirm("Delete all legal metadata for this document?")) return;
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "DELETE"}); await openDocument(document); await queueLegalGraphRebuild(); notify("Legal metadata deleted. A legal graph rebuild has been queued."); }
    catch (error) { showError(error); }
  };
  const openDocument = async document => {
    try {
      const [preview, jobs] = await Promise.all([api(`/v1/documents/${document.id}/text`), api(`/v1/documents/${document.id}/jobs`)]);
      setDocumentPreview({...preview, title: document.title || document.original_filename}); setDocumentJobs(jobs);
    } catch (error) { showError(error); }
  };
  const reprocessDocument = async document => {
    try { await api(`/v1/documents/${document.id}/reprocess`, {method: "POST"}); await loadKbData(selectedKbId); notify("Document queued for reprocessing."); }
    catch (error) { showError(error); }
  };
  const deleteDocument = async document => {
    if (!window.confirm(`Move ${document.title || document.original_filename} to deleted documents?`)) return;
    try { await api(`/v1/documents/${document.id}`, {method: "DELETE"}); await loadKbData(selectedKbId); notify("Document moved to deleted documents."); }
    catch (error) { showError(error); }
  };
  const restoreDocument = async document => {
    try { await api(`/v1/documents/${document.id}/restore`, {method: "POST"}); await loadKbData(selectedKbId, true); notify("Document restored and queued for processing."); }
    catch (error) { showError(error); }
  };
  const reindexEmbeddings = async () => {
    try { const result = await api(`/v1/knowledge-bases/${selectedKbId}/documents/reindex`, {method: "POST"}); await loadKbData(selectedKbId); notify(`${result.count} document(s) queued for embedding reindex.`); }
    catch (error) { showError(error); }
  };
  const loadAccess = async () => {
    const [nextTokens, nextAudit, nextMcpActivity] = await Promise.all([
      api("/v1/tokens"), api("/v1/audit-logs?limit=20"), api("/v1/mcp/activity?limit=50"),
    ]);
    setTokens(nextTokens); setAuditLogs(nextAudit); setMcpActivity(nextMcpActivity);
  };
  const loadTransactionLogs = async (append = false) => {
    const result = await api(`/v1/logs/transactions?limit=50&paginate=true${append && transactionCursor ? `&cursor=${encodeURIComponent(transactionCursor)}` : ""}`);
    setTransactionLogs(current => append ? [...current, ...(result.items || [])] : (result.items || []));
    setTransactionCursor(result.next_cursor || null);
  };
  const loadTraceLogs = async (append = false) => {
    const result = await api(`/v1/traces?limit=50&paginate=true${append && traceCursor ? `&cursor=${encodeURIComponent(traceCursor)}` : ""}`);
    setTraceLogs(current => append ? [...current, ...(result.items || [])] : (result.items || []));
    setTraceCursor(result.next_cursor || null);
  };
  useEffect(() => {
    if (user && activeView === "logs") Promise.all([loadTransactionLogs(), loadTraceLogs()]).catch(showError);
  }, [user, activeView]);
  const createMcpToken = async payload => {
    const result = await api("/v1/tokens", {method: "POST", body: JSON.stringify(payload)});
    await loadAccess(); notify("MCP token created. Copy it now; it will not be shown again."); return result;
  };
  const rotateMcpToken = async tokenId => {
    const result = await api(`/v1/tokens/${tokenId}/rotate`, {method: "POST"});
    await loadAccess(); notify("MCP token rotated. The previous key was revoked; copy the new key now."); return result;
  };
  const changeTokenState = async (tokenId, action) => {
    await api(`/v1/tokens/${tokenId}/${action}`, {method: "POST"}); await loadAccess();
    notify(`Token ${{enable: "enabled", disable: "disabled", revoke: "revoked"}[action] || "updated"}.`);
  };
  const submitQueryFeedback = async (resultId, rating) => {
    try { await api(`/v1/query/results/${resultId}/feedback`, {method: "POST", body: JSON.stringify({rating})}); notify("Thanks — retrieval feedback recorded."); }
    catch (error) { showError(error); }
  };

  if (isSessionLoading) return <main className="login-page"><section className="login-card session-loading"><img className="login-logo" src="/logo-softnix.png" alt="Softnix"/><p className="eyebrow">SOFTNIX · KNOWLEDGE INTELLIGENCE</p><h1>Restoring your session…</h1><p className="login-copy">Checking your secure sign-in.</p></section></main>;
  if (!user) return <Login onLogin={data => setUser(data.user)}/>;
  const switchView = view => { setActiveView(view); setDocumentPreview(null); };
  const sideNav = <SideNav header={<div className="brand-lockup"><img src="/logo-softnix.png" alt="Softnix"/><SideNavHeading superheading="SOFTNIX" heading="Knowledge Intelligence"/></div>} topContent={<Button label="New Knowledge Base" variant="primary" onClick={() => switchView("knowledge-bases")}/>} collapsible>
    <SideNavSection title="KNOWLEDGE" subtitle="Organize your sources" className="side-nav-category">
      <SideNavItem label="Knowledge Bases" isSelected={activeView === "knowledge-bases"} onClick={() => switchView("knowledge-bases")}/>
      <SideNavItem label="Documents" isSelected={activeView === "documents"} onClick={() => switchView("documents")}/>
    </SideNavSection>
    <SideNavSection title="INSIGHTS" subtitle="Find and understand" className="side-nav-category">
      <SideNavItem label="Search" isSelected={activeView === "search"} onClick={() => switchView("search")}/>
      <SideNavItem label="Explore graph" isSelected={activeView === "explore"} onClick={() => switchView("explore")}/>
    </SideNavSection>
    <SideNavSection title="ADMINISTRATION" subtitle="Connect and monitor" className="side-nav-category"><SideNavItem label="Access & MCP" isSelected={activeView === "access"} onClick={() => switchView("access")}/><SideNavItem label="Logging" isSelected={activeView === "logs"} onClick={() => switchView("logs")}/></SideNavSection>
  </SideNav>;
  const topNav = <TopNav label="Workspace navigation" heading={<TopNavHeading heading={selectedKb?.name || "Knowledge workspace"}/>} endContent={<div className="topnav-user"><span className="status-indicator"/> {user.username}</div>}/>;

  return <Theme theme={neutralTheme}><AppShell topNav={topNav} sideNav={sideNav} mobileNav={{breakpoint: "md"}} height="auto" variant="elevated" contentPadding={4}>
    <div className="workspace" aria-live="polite">
      {message && <Toast body={message.body} type={message.type} isAutoHide={message.type !== "error"} autoHideDuration={5000} onDismiss={() => setMessage(null)}/>} 
      {activeView === "knowledge-bases" && <KnowledgeBases kbs={kbs} selectedKbId={selectedKbId} setSelectedKbId={setSelectedKbId} newKbName={newKbName} setNewKbName={setNewKbName} createKb={createKb} manageKnowledgeBase={manageKnowledgeBase} updateRetrievalConfig={updateRetrievalConfig} onContinue={() => switchView("documents")}/>}
      {activeView === "documents" && (
        <Documents selectedKb={selectedKb} documents={documents} showDeletedDocuments={showDeletedDocuments} setShowDeletedDocuments={setShowDeletedDocuments} uploadFile={uploadFile} setUploadFile={setUploadFile} uploadTitle={uploadTitle} setUploadTitle={setUploadTitle} uploadDocumentType={uploadDocumentType} setUploadDocumentType={setUploadDocumentType} uploadDocument={uploadDocument} isUploading={isUploading} openDocument={openDocument} extractLegalMetadata={extractLegalMetadata} saveLegalMetadata={saveLegalMetadata} deleteLegalMetadata={deleteLegalMetadata} reprocessDocument={reprocessDocument} deleteDocument={deleteDocument} restoreDocument={restoreDocument} reindexEmbeddings={reindexEmbeddings} refreshDocuments={() => loadKbData(selectedKbId).catch(showError)} documentPreview={documentPreview} documentJobs={documentJobs} legalInstruments={legalInstruments} resolveLegalRegistry={resolveLegalRegistry} updateLegalInstrument={updateLegalInstrument} onCreateKb={() => switchView("knowledge-bases")} onSearch={() => switchView("search")} onExplore={() => switchView("explore")}/>
      )}
      {activeView === "search" && (
        <SearchView selectedKb={selectedKb} documents={documents} completedDocuments={completedDocuments} query={query} setQuery={setQuery} queryAsOfDate={queryAsOfDate} setQueryAsOfDate={setQueryAsOfDate} queryIncludeHistorical={queryIncludeHistorical} setQueryIncludeHistorical={setQueryIncludeHistorical} runQuery={runQuery} isQuerying={isQuerying} queryResult={queryResult} submitFeedback={submitQueryFeedback} onDocuments={() => switchView("documents")} onOpenSource={document => { switchView("documents"); openDocument(document); }}/>
      )}
      {activeView === "explore" && (
        <ExploreView selectedKb={selectedKb} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
      )}
      {activeView === "access" && <><AccessView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} mcpActivity={mcpActivity} loadAccess={loadAccess} createMcpToken={createMcpToken} rotateMcpToken={rotateMcpToken} changeTokenState={changeTokenState}/><McpActivity activity={mcpActivity}/></>}
      {activeView === "logs" && <LoggingView transactions={transactionLogs} traces={traceLogs} loadTransactions={loadTransactionLogs} loadTraces={loadTraceLogs} hasMoreTransactions={Boolean(transactionCursor)} hasMoreTraces={Boolean(traceCursor)}/>}
    </div>
  </AppShell></Theme>;
}

function LoggingView({transactions, traces, loadTransactions, loadTraces, hasMoreTransactions, hasMoreTraces}) {
  const [search, setSearch] = useState("");
  const [method, setMethod] = useState("all");
  const [status, setStatus] = useState("all");
  const [expandedId, setExpandedId] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [view, setView] = useState("traces");
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const timer = window.setInterval(() => Promise.all([loadTransactions(), loadTraces()]).catch(() => undefined), 10000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadTransactions, loadTraces]);
  const visible = transactions.filter(item => {
    const needle = search.trim().toLocaleLowerCase();
    const matchSearch = !needle || `${item.request_id} ${item.path} ${item.authentication}`.toLocaleLowerCase().includes(needle);
    const matchMethod = method === "all" || item.method === method;
    const matchStatus = status === "all" || (status === "error" ? Number(item.status_code) >= 400 : String(item.status_code).startsWith(status));
    return matchSearch && matchMethod && matchStatus;
  });
  const errors = transactions.filter(item => Number(item.status_code) >= 400).length;
  const mcpRequests = transactions.filter(item => item.path === "/mcp").length;
  const retrievalRequests = transactions.filter(item => item.retrieval?.retrieval_trace?.length).length;
  const averageDuration = transactions.length ? Math.round(transactions.reduce((total, item) => total + Number(item.duration_ms || 0), 0) / transactions.length) : 0;
  const methods = [...new Set(transactions.map(item => item.method).filter(Boolean))].sort();
  return <><PageHeading eyebrow="OPERATIONS" title="Request logging" description="Inspect requests or trace how retrieval decisions, parallel channels, and answer generation were executed. Sensitive request content is never stored." actions={<><label className="log-auto-refresh"><input type="checkbox" checked={autoRefresh} onChange={event => setAutoRefresh(event.target.checked)}/> Auto-refresh</label><Button label="Refresh logs" variant="secondary" onClick={() => Promise.all([loadTransactions(), loadTraces()])}/></>}/>
    <section className="metric-grid"><Metric value={transactions.length} label="Recent transactions" detail="Most recent 250 requests"/><Metric value={errors} label="Errors" detail="HTTP status 4xx and 5xx"/><Metric value={retrievalRequests} label="Retrieval executions" detail={`${mcpRequests} MCP request(s) in this view`}/><Metric value={`${averageDuration} ms`} label="Average duration" detail="Across displayed transactions"/></section>
    <div className="log-tabs" role="tablist"><button role="tab" aria-selected={view === "traces"} className={view === "traces" ? "selected" : ""} onClick={() => setView("traces")}>Trace Explorer</button><button role="tab" aria-selected={view === "transactions"} className={view === "transactions" ? "selected" : ""} onClick={() => setView("transactions")}>All transactions</button></div>
    {view === "traces" ? <><TraceExplorer traces={traces}/>{hasMoreTraces && <div className="log-load-more"><Button label="Load older traces" variant="secondary" onClick={() => loadTraces(true)}/></div>}</> : <Card padding={4}><div className="log-filter-bar"><TextInput label="Find a request" value={search} onChange={setSearch} placeholder="Request ID, route, or authentication"/><DesignSystemSelect label="Method" value={method} onChange={setMethod} options={[{value: "all", label: "All methods"}, ...methods.map(value => ({value, label: value}))]}/><DesignSystemSelect label="Status" value={status} onChange={setStatus} options={[{value: "all", label: "All statuses"}, {value: "2", label: "2xx success"}, {value: "4", label: "4xx client error"}, {value: "5", label: "5xx server error"}, {value: "error", label: "All errors"}]}/></div>
      <p className="section-copy log-privacy-note">Protected data: request bodies, prompt content, cookies, authorization headers, and token values are excluded from this log.</p>
      {visible.length ? <div className="transaction-list">{visible.map(item => {
        const isOpen = expandedId === item.id;
        const isError = Number(item.status_code) >= 400;
        const execution = item.retrieval;
        return <article className={`transaction-row ${isError ? "has-error" : ""}`} key={item.id}><button type="button" className="transaction-summary" onClick={() => setExpandedId(isOpen ? null : item.id)} aria-expanded={isOpen}><span className="transaction-route"><b className={`http-method ${item.method?.toLowerCase()}`}>{item.method}</b><code>{item.path}</code><small>{new Date(item.created_at).toLocaleString()} · {item.authentication}{execution ? " · retrieval trace" : ""}</small></span><span className={`transaction-status ${isError ? "error" : ""}`}>{item.status_code}</span><span className="transaction-duration">{item.duration_ms} ms</span></button>{isOpen && <div className="transaction-detail"><div><span>Request ID</span><code>{item.request_id}</code></div><div><span>Transaction</span><code>{item.method} {item.path} → {item.status_code} in {item.duration_ms} ms</code></div>{execution && <RetrievalExecutionTrace execution={execution}/>}<p>Use the request ID to correlate this entry with structured service logs and MCP activity. No request content is retained.</p></div>}</article>;
      })}</div> : <EmptyState isCompact title="No matching transactions" description="Try removing a filter or refresh the logs."/>}{hasMoreTransactions && <div className="log-load-more"><Button label="Load older transactions" variant="secondary" onClick={() => loadTransactions(true)}/></div>}
    </Card>}</>;
}

function RetrievalExecutionTrace({execution}) {
  const plan = execution.retrieval_plan || {};
  const trace = execution.retrieval_trace || [];
  return <section className="retrieval-execution"><div className="retrieval-execution-heading"><span>Retrieval execution</span><b>{plan.intent || execution.tool || "retrieval"}</b></div>{plan.channels?.length > 0 && <p className="retrieval-plan-summary">{plan.planner_source || "rules"} · {plan.channels.join(", ")}{plan.fallback_reason ? " · deterministic fallback" : ""}</p>}<ul className="retrieval-execution-list">{trace.map((step, index) => <li key={`${step.channel}-${step.system}-${index}`}><i className={`mcp-step-dot ${step.status}`}/><div><b>{step.channel}</b><small>{step.system} · {step.status} · {step.result_count || 0} result(s) · {step.duration_ms || 0} ms</small>{step.detail && <span>{step.detail}</span>}</div></li>)}</ul></section>;
}

function TraceExplorer({traces}) {
  const [selectedTraceId, setSelectedTraceId] = useState("");
  const [trace, setTrace] = useState(null);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    if (!selectedTraceId && traces[0]?.trace_id) setSelectedTraceId(traces[0].trace_id);
    if (selectedTraceId && !traces.some(item => item.trace_id === selectedTraceId)) setSelectedTraceId(traces[0]?.trace_id || "");
  }, [traces, selectedTraceId]);
  useEffect(() => {
    if (!selectedTraceId) { setTrace(null); return undefined; }
    let active = true; setLoading(true);
    api(`/v1/traces/${selectedTraceId}`).then(value => { if (active) setTrace(value); }).catch(() => { if (active) setTrace(null); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [selectedTraceId]);
  const visible = traces.filter(item => {
    const needle = filter.trim().toLocaleLowerCase();
    return !needle || `${item.trace_id} ${item.tool || ""} ${item.intent || ""} ${item.transport}`.toLocaleLowerCase().includes(needle);
  });
  return <section className="trace-explorer"><aside className="trace-list-pane"><div className="trace-list-heading"><div><p className="eyebrow">TRACE EXPLORER</p><h2>Retrieval runs</h2></div><span>{visible.length}</span></div><TextInput label="Find a trace" value={filter} onChange={setFilter} placeholder="Tool, intent, or trace ID"/>
    <div className="trace-list">{visible.map(item => <button type="button" key={item.trace_id} className={`trace-summary ${item.trace_id === selectedTraceId ? "selected" : ""}`} onClick={() => setSelectedTraceId(item.trace_id)}><span className={`trace-status-dot ${item.status}`}/><span><b>{item.tool || "Search knowledge"}</b><small>{item.intent || "retrieval"} · {item.transport}</small><small>{new Date(item.created_at).toLocaleString()}</small></span><em>{item.duration_ms} ms</em></button>)}</div>
  </aside><main className="trace-detail-pane">{loading ? <p className="section-copy">Loading trace…</p> : trace ? <TraceWaterfall trace={trace}/> : <EmptyState isCompact title="Choose a retrieval trace" description="Select a completed Search or MCP request to inspect its execution."/>}</main></section>;
}

function TraceStatusLabel({status, reasonCode}) {
  if (status === "skipped") return reasonCode === "not_selected_by_plan" ? "Skipped by plan" : "Skipped / unavailable";
  if (status === "unavailable") return "Failed / unavailable";
  if (status === "used") return "Completed";
  return status || "Unknown";
}

function TraceOverview({trace}) {
  const request = trace.request_summary || {};
  const response = trace.response_summary || {};
  return <div className="trace-overview-grid">
    <section className="trace-payload-card"><p className="eyebrow">REQUEST</p><div className="trace-query-preview">{request.query_preview || "Query preview unavailable"}</div><dl className="trace-meta-list"><div><dt>Length</dt><dd>{request.query_length || 0} characters</dd></div><div><dt>SHA-256</dt><dd><code>{request.query_sha256 || "—"}</code></dd></div><div><dt>Filters</dt><dd>{Object.keys(request.filter_summary || {}).length ? JSON.stringify(request.filter_summary) : "None"}</dd></div></dl><p className="trace-safe-note">Bounded preview for administrators; full prompts and headers are never stored.</p></section>
    <section className="trace-payload-card"><p className="eyebrow">RESPONSE</p><div className="trace-answer-preview">{response.answer_preview || "No answer preview"}</div><dl className="trace-meta-list"><div><dt>Status</dt><dd>{response.status || trace.status}</dd></div><div><dt>Sources</dt><dd>{response.source_count ?? trace.source_count ?? 0} cited source(s)</dd></div><div><dt>Entities / relationships</dt><dd>{response.entity_count ?? 0} / {response.relationship_count ?? 0}</dd></div></dl><p className="trace-safe-note">Answer text is truncated and redacted for audit use.</p></section>
  </div>;
}

function TraceDecision({trace}) {
  const plan = trace.retrieval_plan || {};
  return <section className="trace-decision"><div className="trace-decision-row"><span>Intent</span><strong>{plan.intent || trace.intent || "retrieval"}</strong></div><div className="trace-decision-row"><span>Decision source</span><strong>{plan.planner_source || "rules"}{plan.policy_version ? ` · policy v${plan.policy_version}` : ""}</strong></div><div className="trace-decision-row"><span>Why this route</span><strong>{plan.rationale || "No planner rationale was recorded."}</strong></div><div className="trace-decision-row"><span>Selected channels</span><strong>{plan.channels?.length ? plan.channels.join(" → ") : "None"}</strong></div><div className="trace-decision-row"><span>Limits</span><strong>top {plan.max_sources || "—"} · graph depth {plan.graph_depth || "—"} · {plan.graph_scope || "none"} scope</strong></div>{plan.fallback_reason && <div className="trace-decision-warning">Deterministic fallback: {plan.fallback_reason}</div>}<p className="trace-safe-note">A channel marked “Skipped by plan” is an intentional decision, not a runtime failure.</p></section>;
}

function TraceTimeline({trace}) {
  const total = Math.max(trace.duration_ms || 0, 1);
  const spans = (trace.spans || []).map(span => {
    const left = Math.min(100, (Number(span.offset_ms || 0) / total) * 100);
    const width = Math.max(1.5, Math.min(100 - left, (Number(span.duration_ms || 0) / total) * 100));
    return <details className="waterfall-row" key={span.span_id}>
      <summary><div><b>{span.channel}</b><small>{span.system}</small></div><div className="waterfall-track"><span className={`waterfall-bar ${span.status}`} style={{left: `${left}%`, width: `${width}%`}} title={`${span.offset_ms}–${Number(span.offset_ms || 0) + Number(span.duration_ms || 0)} ms`}/></div><div><em>{span.duration_ms} ms</em><small>{TraceStatusLabel({status: span.status, reasonCode: span.reason_code})} · {span.result_count || 0} results</small></div></summary>
      {span.detail && <p>{span.detail}</p>}
      <div className="trace-span-details"><dl><div><dt>Input</dt><dd>query {span.input_summary?.query_sha256 ? `sha ${span.input_summary.query_sha256.slice(0, 12)}…` : "summary unavailable"}; {span.input_summary?.knowledge_base_count ?? 0} KB(s); max {span.input_summary?.max_sources ?? "—"} sources</dd></div><div><dt>Output</dt><dd>{span.output_summary?.result_count ?? span.result_count ?? 0} result(s); {span.output_summary?.status || span.status}</dd></div>{span.reason_code && <div><dt>Reason</dt><dd>{TraceStatusLabel({status: span.status, reasonCode: span.reason_code})}</dd></div>}</dl></div>
    </details>;
  });
  return <section className="waterfall-panel"><div className="waterfall-heading"><div>Execution spans</div><span>0 ms</span><span>{trace.duration_ms} ms</span></div><div className="waterfall-root"><b>{trace.root_span?.name || "Request"}</b><span className={`waterfall-root-bar ${trace.status}`}/><em>{trace.duration_ms} ms</em></div><div className="waterfall-spans">{spans}</div></section>;
}

function TraceEvidence({trace}) {
  const ids = trace.response_summary?.citation_ids || [];
  return <section className="trace-evidence"><p className="eyebrow">CITATIONS</p><h3>{ids.length ? `${ids.length} cited source(s)` : "No citations returned"}</h3>{ids.length ? <div className="trace-chip-list">{ids.map(id => <span className="trace-chip" key={id}>{id}</span>)}</div> : <p className="trace-empty-note">The executor did not return evidence for this request. Check the response summary and channel spans for the reason.</p>}<p className="trace-safe-note">Citation identifiers are retained so an operator can reconcile this trace with the cited Search/MCP response without storing document bodies.</p></section>;
}

function TraceWaterfall({trace}) {
  const [tab, setTab] = useState("overview");
  const plan = trace.retrieval_plan || {};
  const tabs = [["overview", "Overview"], ["decision", "Decision"], ["timeline", "Timeline"], ["evidence", "Evidence"]];
  return <div className="trace-waterfall">
    <div className="trace-detail-heading"><div><p className="eyebrow">{trace.transport === "mcp" ? "MCP TRACE" : "SEARCH TRACE"}</p><h2>{trace.root_span?.name || "Knowledge query"}</h2><p>{trace.intent || "retrieval"} · {trace.source_count} cited source(s) · {trace.duration_ms} ms</p></div><span className={`trace-status ${trace.status}`}>{trace.status}</span></div>
    <section className="trace-context"><div><span>Trace ID</span><code>{trace.trace_id}</code></div><div><span>Scope</span><code>{trace.knowledge_base_ids?.length || 0} Knowledge Base(s)</code></div><div><span>Planner</span><code>{plan.planner_source || "rules"}</code></div></section>
    <nav className="trace-tabs" aria-label="Trace detail views">{tabs.map(([value, label]) => <button type="button" role="tab" aria-selected={tab === value} className={tab === value ? "selected" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}</nav>
    {tab === "overview" && <TraceOverview trace={trace}/>} {tab === "decision" && <TraceDecision trace={trace}/>} {tab === "timeline" && <TraceTimeline trace={trace}/>} {tab === "evidence" && <TraceEvidence trace={trace}/>}
  </div>;
}

const PageHeading = ({eyebrow, title, description, actions}) => <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</div>;
const STATUS_LABELS = {queued: "Waiting to start", extracting: "Reading text", indexing: "Building search index", completed: "Ready", failed: "Needs attention", ocr_required: "OCR required", disabled: "Disabled"};
const STATUS_HELP = {queued: "Waiting for the processing worker.", extracting: "Extracting text and checking the document.", indexing: "Preparing semantic, keyword, and graph search.", failed: "Processing stopped. Open the document to see the reason.", ocr_required: "This PDF has no text layer. OCR is required before it can be searched."};
const StatusBadge = ({status}) => <Badge label={STATUS_LABELS[status] || status.replace(/_/g, " ")} variant={status === "completed" ? "success" : status === "failed" || status === "ocr_required" ? "error" : status === "queued" || status === "extracting" || status === "indexing" ? "warning" : "neutral"}/>;
const Metric = ({value, label, detail}) => <Card padding={3}><p className="metric-value">{value}</p><p className="metric-label">{label}</p>{detail && <p className="metric-detail">{detail}</p>}</Card>;

const KB_AVATAR_COLORS = ["#0d6874", "#2f6690", "#c97b2c", "#5b6ee1", "#2f9e6f", "#a8467a"];
const kbInitial = name => (name || "").trim().charAt(0).toUpperCase() || "?";

function KnowledgeBases({kbs, selectedKbId, setSelectedKbId, newKbName, setNewKbName, createKb, manageKnowledgeBase, updateRetrievalConfig, onContinue}) {
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [isCreating, setIsCreating] = useState(false);
  const normalizedSearch = searchTerm.trim().toLocaleLowerCase();
  const visibleKnowledgeBases = kbs.filter(kb => {
    const matchesSearch = !normalizedSearch || `${kb.name} ${kb.code}`.toLocaleLowerCase().includes(normalizedSearch);
    return matchesSearch && (statusFilter === "all" || kb.status === statusFilter);
  });
  const openKnowledgeBase = kb => { setSelectedKbId(kb.id); onContinue(); };
  const submitCreate = event => { createKb(event); setIsCreating(false); };
  return <>
    <section className="kb-hero">
      <p className="eyebrow">KNOWLEDGE BASES</p>
      <h1>Choose a Knowledge Base</h1>
      <p>Every search, upload, and graph view starts with a Knowledge Base. Pick one to continue, or create a new one.</p>
      <div className="kb-hero-search">
        <TextInput label="Search Knowledge Bases" value={searchTerm} onChange={setSearchTerm} placeholder="Search by name or code"/>
        <label className="native-field">Status<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">All statuses</option><option value="active">Active</option><option value="draft">Draft</option><option value="disabled">Disabled</option></select></label>
      </div>
    </section>
    <div className="kb-hub-heading"><h2>Your Knowledge Bases</h2><p>{kbs.length ? `${visibleKnowledgeBases.length} of ${kbs.length} shown` : "Create your first one below"}</p></div>
    <section className="kb-hub-grid">
      {visibleKnowledgeBases.map((kb, index) => <article className={`kb-hub-card ${kb.id === selectedKbId ? "selected" : ""}`} key={kb.id}>
        <button type="button" className="kb-hub-open" onClick={() => openKnowledgeBase(kb)}>
          <span className="kb-hub-avatar" style={{background: KB_AVATAR_COLORS[index % KB_AVATAR_COLORS.length]}}>{kbInitial(kb.name)}</span>
          <span className="kb-hub-title">{kb.name}</span>
          <span className="kb-hub-code">{kb.code}</span>
          <StatusBadge status={kb.status}/>
          <span className="kb-hub-link">Open Knowledge Base →</span>
        </button>
        <div className="kb-hub-actions">
          {kb.status === "active" ? <Button label="Disable" size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "disable")}/> : <Button label="Activate" size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "activate")}/>}
          <Button label="Delete" size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "delete")}/>
        </div>
        <RetrievalPolicyEditor knowledgeBase={kb} onSave={config => updateRetrievalConfig(kb, config)}/>
      </article>)}
      <article className={`kb-hub-card kb-hub-create ${isCreating ? "open" : ""}`}>
        {isCreating
          ? <form className="form-stack" onSubmit={submitCreate}><TextInput label="Knowledge Base name" value={newKbName} onChange={setNewKbName} placeholder="e.g. IT Architecture" isRequired hasAutoFocus/><div className="kb-hub-create-actions"><Button label="Cancel" type="button" variant="ghost" size="sm" onClick={() => setIsCreating(false)}/><Button label="Create" type="submit" variant="primary" size="sm"/></div></form>
          : <button type="button" className="kb-hub-create-trigger" onClick={() => setIsCreating(true)}><span className="kb-hub-create-icon">+</span><span>New Knowledge Base</span></button>}
      </article>
    </section>
    {kbs.length > 0 && !visibleKnowledgeBases.length && <EmptyState isCompact title="No Knowledge Bases match" description="Try another name, code, or status filter."/>}
  </>;
}

function RetrievalPolicyEditor({knowledgeBase, onSave}) {
  const current = knowledgeBase.retrieval_config || {};
  const [draft, setDraft] = useState(current);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(current), [knowledgeBase.id, knowledgeBase.retrieval_config]);
  const toggle = key => setDraft(value => ({...value, [key]: !value[key]}));
  const save = async event => { event.preventDefault(); setSaving(true); try { await onSave(draft); } finally { setSaving(false); } };
  return <details className="retrieval-policy"><summary>Retrieval policy</summary><form className="retrieval-policy-form" onSubmit={save}><p className="section-copy">Controls which stores the planner may use for this Knowledge Base.</p><DesignSystemSelect label="Mode" value={draft.retrieval_mode || "auto"} onChange={value => setDraft({...draft, retrieval_mode: value})} options={[{value: "auto", label: "Auto"}, {value: "balanced", label: "Balanced"}, {value: "precision", label: "Precision"}, {value: "recall", label: "Recall"}]}/><div className="policy-checks">{[["enable_vector","Semantic vector"],["enable_fulltext","Full-text"],["enable_graph","Graph"],["enable_lightrag","LightRAG"],["enable_reranker","Reranker"],["planner_llm_fallback","LLM fallback for ambiguous queries"]].map(([key,label]) => <DesignSystemCheckbox key={key} label={label} checked={draft[key] !== false} onChange={() => toggle(key)}/>)}</div><div className="policy-numbers"><label>Default top-k<input type="number" min="1" max="30" value={draft.default_top_k || 12} onChange={event => setDraft({...draft, default_top_k: Number(event.target.value)})}/></label><label>Graph depth<input type="number" min="1" max="3" value={draft.maximum_graph_depth || 3} onChange={event => setDraft({...draft, maximum_graph_depth: Number(event.target.value)})}/></label></div><Button label="Save retrieval policy" type="submit" size="sm" variant="secondary" isLoading={saving}/></form></details>;
}

function Documents({selectedKb, documents, showDeletedDocuments, setShowDeletedDocuments, uploadFile, setUploadFile, uploadTitle, setUploadTitle, uploadDocumentType, setUploadDocumentType, uploadDocument, isUploading, openDocument, extractLegalMetadata, saveLegalMetadata, deleteLegalMetadata, reprocessDocument, deleteDocument, restoreDocument, reindexEmbeddings, refreshDocuments, documentPreview, documentJobs, legalInstruments, resolveLegalRegistry, updateLegalInstrument, onCreateKb, onSearch, onExplore}) {
  if (!selectedKb) return <EmptyState title="Create a Knowledge Base first" description="Documents need a context so search results remain relevant and secure." actions={<Button label="Create Knowledge Base" variant="primary" onClick={onCreateKb}/>}/>;
  return <><PageHeading eyebrow="DOCUMENTS" title={`Build ${selectedKb.name}`} description="Drag in a file. We validate it, extract its text, prepare citations, and make it searchable." actions={<><Button label={showDeletedDocuments ? "Hide deleted" : "Show deleted"} variant="ghost" onClick={() => setShowDeletedDocuments(value => !value)}/><Button label="Reindex embeddings" variant="secondary" onClick={reindexEmbeddings}/><Button label="Refresh status" variant="ghost" onClick={refreshDocuments}/></>}/>
    <Card padding={4} variant="muted"><form className="upload-layout" onSubmit={uploadDocument}><FileInput label="Add documents" value={uploadFile} onChange={files => setUploadFile(Array.isArray(files) ? files : files ? [files] : [])} isMultiple maxFiles={20} accept={ACCEPTED_FILES} maxSize={MAX_FILE_SIZE} mode="dropzone" description="Select up to 20 files · PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML, CSV, or JSON · up to 50 MB each" isLoading={isUploading}/><div className="upload-meta"><DesignSystemSelect label="Document type" value={uploadDocumentType} onChange={setUploadDocumentType} options={DOCUMENT_TYPE_OPTIONS.map(({value, label}) => ({value, label}))} isDisabled={isUploading} size="md"/><p className="section-copy document-type-help">{DOCUMENT_TYPE_OPTIONS.find(option => option.value === uploadDocumentType)?.description} · applies to every selected file</p><TextInput label="Document title" value={uploadTitle} onChange={setUploadTitle} placeholder="Optional display title (single file only)" isOptional isDisabled={uploadFile.length !== 1 || isUploading}/>{uploadFile.length > 1 && <p className="section-copy document-type-help">Batch upload uses each original filename as its document title.</p>}<Button label={uploadFile.length > 1 ? `Upload ${uploadFile.length} files and process` : "Upload and process"} type="submit" variant="primary" isDisabled={!uploadFile.length} isLoading={isUploading}/></div></form><p className="section-copy upload-format-note">Each file becomes its own processing job. A failed file can be retried manually without reprocessing the rest. Office files are converted to structured Markdown for search, citations, and legal review.</p></Card>
    <section className="content-section"><div className="section-title"><div><h2>{showDeletedDocuments ? "All documents" : "Library"}</h2><p>{documents.length ? `${documents.length} document${documents.length === 1 ? "" : "s"} in this Knowledge Base` : "Your uploaded documents will appear here."}</p></div>{documents.some(document => ["queued", "extracting", "indexing"].includes(document.status)) && <span className="live-status" role="status">Updating automatically</span>}</div>{documents.length ? <div className="document-table">{documents.map(document => <article key={document.id} className="document-item"><div className="document-main"><button className="document-title" onClick={() => openDocument(document)}>{document.title || document.original_filename}</button><p>{document.original_filename} · {Math.ceil(document.file_size / 1024)} KB · {documentTypeLabel(document.document_type)}</p>{["queued", "extracting", "indexing"].includes(document.status) && <><ProgressBar label={`${document.title || document.original_filename} processing`} value={100} variant="warning" isIndeterminate/><p className="document-status-help">{STATUS_HELP[document.status]}</p></>}{STATUS_HELP[document.status] && ["failed", "ocr_required"].includes(document.status) && <p className="document-status-help document-status-warning">{STATUS_HELP[document.status]}{document.error_code ? ` (${document.error_code})` : ""}</p>}</div><StatusBadge status={document.status}/><div className="document-actions"><Button label="Open details" variant="ghost" size="sm" onClick={() => openDocument(document)}/>{document.deleted_at ? <Button label="Restore" variant="secondary" size="sm" onClick={() => restoreDocument(document)}/> : <><Button label="Process again" variant="secondary" size="sm" isDisabled={["queued", "extracting", "indexing"].includes(document.status)} onClick={() => reprocessDocument(document)}/><Button label="Delete" variant="destructive" size="sm" onClick={() => deleteDocument(document)}/></>}</div></article>)}</div> : <EmptyState title="Your library is ready for its first document" description="Use the drop zone above. We will show processing progress and tell you if anything needs attention."/>}</section>
    {documents.some(document => document.status === "completed") && <section className="next-step-card"><div><p className="eyebrow">NEXT STEP</p><h2>Your knowledge is ready to use</h2><p>Ask a question for cited answers, or explore the entities and relationships found in your documents.</p></div><div className="next-step-actions"><Button label="Search knowledge" variant="primary" onClick={onSearch}/><Button label="Explore graph" variant="secondary" onClick={onExplore}/></div></section>}
    {legalInstruments?.length > 0 && <LegalRegistryPanel instruments={legalInstruments} resolveLegalRegistry={resolveLegalRegistry} updateLegalInstrument={updateLegalInstrument}/>}
    {documentPreview && <DocumentPreview preview={documentPreview} jobs={documentJobs} legalInstrument={legalInstruments?.find(row => row.document_id === documentPreview.document_id)} onExtractLegal={extractLegalMetadata} onSaveLegal={saveLegalMetadata} onDeleteLegal={deleteLegalMetadata} onUpdateLegalInstrument={updateLegalInstrument}/>}</>
}

const LEGAL_KIND_LABELS_TH = {
  constitution: "รัฐธรรมนูญ", act: "พระราชบัญญัติ", royal_decree: "พระราชกฤษฎีกา", ministerial_regulation: "กฎกระทรวง",
  notification: "ประกาศ", rule: "ระเบียบ/ข้อบังคับ", circular: "หนังสือเวียน", guideline: "แนวปฏิบัติ/คู่มือ",
  resolution: "มติ", contract: "สัญญา", faq: "FAQ", other: "อื่น ๆ",
};
const LEGAL_STATUS_LABELS_TH = {
  in_force: "บังคับใช้", amended: "แก้ไขเพิ่มเติมแล้ว", superseded: "ถูกแทนที่", repealed: "ถูกยกเลิก",
  not_yet_effective: "ยังไม่มีผลบังคับใช้", unknown: "ไม่ทราบสถานะ",
};
const LEGAL_CLASS_LABELS_TH = {
  consolidated: "ฉบับรวม/ปรับปรุง", amendment: "ฉบับแก้ไขเพิ่มเติม", original: "ฉบับหลัก",
};

const legalStatusLabel = status => LEGAL_STATUS_LABELS_TH[status] || "ไม่ทราบสถานะ";
const legalClassLabel = instrument => LEGAL_CLASS_LABELS_TH[instrument.document_class] || LEGAL_KIND_LABELS_TH[instrument.kind] || "ตราสารกฎหมาย";
const legalDateValue = instrument => instrument.effective_from || instrument.version_date || "";
const legalDateLabel = instrument => {
  if (!instrument.effective_from) return instrument.version_date ? `ฉบับวันที่ ${instrument.version_date}` : "ยังไม่ระบุวันที่";
  return `มีผล ${instrument.effective_from}`;
};

function LegalRegistryPanel({instruments, resolveLegalRegistry, updateLegalInstrument}) {
  const [editingId, setEditingId] = useState(null);
  return <section className="content-section legal-registry-panel"><div className="section-title"><div><h2>Legal registry</h2><p>{instruments.length} legal instrument{instruments.length === 1 ? "" : "s"} tracked · status, authority, dates, and provenance drive retrieval ranking.</p></div><Button label="Resolve statuses" variant="secondary" size="sm" onClick={resolveLegalRegistry}/></div>
    <div className="document-table">{instruments.map(row => <article key={row.id} className="document-item legal-registry-row"><div className="document-main"><span className="document-title">{row.official_title || row.document_id}</span><p>{LEGAL_KIND_LABELS_TH[row.kind] || row.kind} · authority {row.authority_level}{row.version_label ? ` · ${row.version_label}` : ""}{row.effective_from ? ` · มีผล ${row.effective_from}` : ""}{row.effective_to ? ` ถึง ${row.effective_to}` : ""}</p><p className="section-copy">Review: {row.review_status || "unreviewed"} · Source: {row.source_reference || row.source_uri || "ยังไม่ระบุ"}</p>{row.status_reason && <p className="section-copy">{row.status_reason}</p>}</div><LegalStatusBadge status={row.status}/><div className="document-actions"><Button label={editingId === row.id ? "Cancel" : "Review / override"} variant="ghost" size="sm" onClick={() => setEditingId(editingId === row.id ? null : row.id)}/></div>
      {editingId === row.id && <LegalInstrumentOverrideForm row={row} onSave={payload => { updateLegalInstrument(row.id, payload); setEditingId(null); }}/>}
    </article>)}</div>
  </section>;
}

function LegalInstrumentOverrideForm({row, onSave}) {
  const [status, setStatus] = useState(row.status);
  const [effectiveFrom, setEffectiveFrom] = useState(row.effective_from || "");
  const [effectiveTo, setEffectiveTo] = useState(row.effective_to || "");
  const [sourceUri, setSourceUri] = useState(row.source_uri || "");
  const [sourceReference, setSourceReference] = useState(row.source_reference || "");
  const submit = event => { event.preventDefault(); onSave({status, effective_from: effectiveFrom || null, effective_to: effectiveTo || null, source_uri: sourceUri || null, source_reference: sourceReference || null}); };
  return <form className="legal-override-form" onSubmit={submit}><label className="native-field">Status<select value={status} onChange={event => setStatus(event.target.value)}>{Object.keys(LEGAL_STATUS_LABELS_TH).map(value => <option key={value} value={value}>{LEGAL_STATUS_LABELS_TH[value]}</option>)}</select></label><label className="native-field">Effective from<input type="date" value={effectiveFrom} onChange={event => setEffectiveFrom(event.target.value)}/></label><label className="native-field">Effective to<input type="date" value={effectiveTo} onChange={event => setEffectiveTo(event.target.value)}/></label><label className="native-field">Official source URL<input value={sourceUri} onChange={event => setSourceUri(event.target.value)} placeholder="https://..."/></label><label className="native-field">Source reference<input value={sourceReference} onChange={event => setSourceReference(event.target.value)} placeholder="Gazette / contract reference"/></label><Button label="Save review" type="submit" size="sm" variant="primary"/><p className="section-copy">Saving marks the instrument as manually verified. Automatic status resolution will not overwrite it.</p></form>;
}

function SearchView({selectedKb, documents, completedDocuments, query, setQuery, queryAsOfDate, setQueryAsOfDate, queryIncludeHistorical, setQueryIncludeHistorical, runQuery, isQuerying, queryResult, submitFeedback, onDocuments, onOpenSource}) {
  if (!selectedKb) return <EmptyState title="Select a Knowledge Base to search" description="Create a Knowledge Base and upload documents first." actions={<Button label="Go to documents" variant="primary" onClick={onDocuments}/>}/>;
  if (!completedDocuments) return <EmptyState title="Finish preparing a document first" description={documents.length ? "Your document is still being processed. Return to Documents to follow its progress." : "Upload a document to create searchable knowledge for this Knowledge Base."} actions={<Button label={documents.length ? "View processing" : "Upload document"} variant="primary" onClick={onDocuments}/>}/>;
  const examples = ["What systems depend on the database?", "Summarize the main architecture decisions.", "What is the impact if this service stops working?"];
  return <><PageHeading eyebrow="SEARCH" title="Ask your knowledge" description={`Answers search ${selectedKb.name} and always show the evidence they are based on.`}/><Card padding={4} variant="blue"><form className="search-form" onSubmit={runQuery}><TextArea label="Your question" value={query} onChange={setQuery} rows={4} placeholder="Ask a clear question about this Knowledge Base" isRequired/><div className="example-row"><span>Try an example:</span>{examples.map(example => <button key={example} type="button" className="example-chip" onClick={() => setQuery(example)}>{example}</button>)}</div>
    <details className="legal-query-filters"><summary>Legal date filters (optional)</summary><div className="legal-query-filters-row"><label className="native-field">As of date<input type="date" value={queryAsOfDate} onChange={event => setQueryAsOfDate(event.target.value)}/></label><label><input type="checkbox" checked={queryIncludeHistorical} onChange={event => setQueryIncludeHistorical(event.target.checked)}/> Include repealed/superseded versions</label></div><p className="section-copy">Leave the date empty to use today. Legal instrument status only affects results for Knowledge Bases with a legal registry.</p></details>
    <Button label={isQuerying ? "Searching knowledge…" : "Search knowledge"} type="submit" variant="primary" size="lg" isDisabled={!query.trim() || isQuerying} isLoading={isQuerying}/>{isQuerying && <p className="query-progress" role="status" aria-live="polite">กำลังค้นหาความหมาย คีย์เวิร์ด ความสัมพันธ์ และแหล่งอ้างอิง…</p>}</form></Card>{queryResult && <QueryResult data={queryResult} submitFeedback={submitFeedback} onOpenSource={onOpenSource}/>}</>;
}

function ExploreView({selectedKb, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship}) {
  if (!selectedKb) return <EmptyState title="Choose a Knowledge Base to explore" description="Relationships and impact analysis are scoped to one Knowledge Base."/>;
  const description = isLegalGraph ? "Verified legal structure is shown by default. Review suggested cross-document relationships with their evidence before approving them." : "Click a blank area to add an entity. Drag from any edge of a node to another node to connect them.";
  return <><PageHeading eyebrow="EXPLORE" title={isLegalGraph ? "Explore your legal knowledge graph" : "Explore your knowledge graph"} description={description}/>
    <GraphWorkspace knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
  </>;
}

const ENTITY_TYPES = ["Application", "Service", "Server", "Database", "BusinessProcess", "Organization", "Concept"];
const LEGAL_ENTITY_TYPES = ["LegalInstrument", "Provision", "LegalAuthority", "LegalParty", "Obligation", "Right", "Prohibition", "Penalty", "Definition", "Amendment"];
const RELATIONSHIP_TYPES = ["DEPENDS_ON", "RUNS_ON", "USES", "SUPPORTS", "AFFECTS"];

function KnowledgeNode({data, selected}) {
  const isPerson = /person|people|organization|user|team/i.test(data.entityType);
  const handles = [
    ["top", Position.Top],
    ["right", Position.Right],
    ["bottom", Position.Bottom],
    ["left", Position.Left],
  ];
  return <div className={`knowledge-node graph-visual-node ${isPerson ? "person-node" : "asset-node"} ${selected ? "selected" : ""}`}>
    {handles.map(([id, position]) => <Handle key={id} id={id} type="source" position={position} className={`graph-handle graph-handle-${id}`}/>) }
    <div className="graph-node-circle" title={data.entityType}>{isPerson ? <span className="person-glyph"><i/><b/></span> : <span className="asset-glyph"><i/><i/><i/><i/><i/><i/></span>}</div>
    <strong className="graph-node-label">{data.label}</strong>
    <span className="graph-node-type">{data.entityType}{data.documentId ? ` · ${String(data.documentId).slice(0, 8)}` : ""}</span>
  </div>;
}

const graphNodeTypes = {knowledge: KnowledgeNode};

function LegalMapPanel({map, loading, onSelectInstrument, onOpenAdvanced}) {
  const instruments = map?.instruments || [];
  const instrumentById = Object.fromEntries(instruments.map(item => [item.id, item]));
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("current");
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const matchesSearch = instrument => !normalizedSearch || [instrument.title, instrument.filename, instrument.kind, instrument.document_class]
    .filter(Boolean).join(" ").toLocaleLowerCase().includes(normalizedSearch);
  const sortByDate = (left, right) => String(legalDateValue(right)).localeCompare(String(legalDateValue(left)));
  const current = instruments.filter(item => item.status === "in_force").sort(sortByDate);
  const currentConsolidated = current.filter(item => item.document_class === "consolidated");
  const currentInstrument = currentConsolidated[0] || current[0] || instruments.slice().sort(sortByDate)[0];
  const historical = instruments.filter(item => item.status !== "in_force").sort(sortByDate);
  const filteredCurrent = current.filter(matchesSearch);
  const filteredHistorical = historical.filter(matchesSearch);
  const visible = statusFilter === "current" ? filteredCurrent : statusFilter === "historical" ? filteredHistorical : instruments.filter(matchesSearch).sort(sortByDate);
  const grouped = visible.reduce((groups, item) => {
    const key = item.document_class || (item.status === "in_force" ? "current" : "historical");
    (groups[key] ||= []).push(item);
    return groups;
  }, {});
  const nodeCount = instruments.reduce((sum, item) => sum + (item.entity_count || 0), 0);
  const relationCount = instruments.reduce((sum, item) => sum + (item.relationship_count || 0), 0);
  const renderCard = instrument => <button key={instrument.id} type="button" className={`legal-instrument-card ${instrument.id === currentInstrument?.id ? "is-current" : ""}`} onClick={() => onSelectInstrument(instrument.id)}>
    <div className="legal-instrument-card-top"><span className="legal-class-chip">{legalClassLabel(instrument)}</span><span className={`legal-status ${instrument.status || "unknown"}`}><i aria-hidden="true"/>{legalStatusLabel(instrument.status)}</span></div>
    <h3>{instrument.title}</h3>
    <p className="legal-instrument-filename">{instrument.filename || instrument.document_id}</p>
    <div className="legal-instrument-metrics"><span><strong>{instrument.entity_count || 0}</strong> โหนด</span><span><strong>{instrument.relationship_count || 0}</strong> ความสัมพันธ์</span></div>
    <div className="legal-instrument-version">{legalDateLabel(instrument)}{instrument.version_label ? ` · ${instrument.version_label}` : ""}</div>
    <span className="legal-instrument-open">เปิดโครงสร้างเอกสาร <b>→</b></span>
  </button>;
  const timelineFamilies = (map?.families || []).map(family => ({...family, items: (family.instrument_ids || []).map(id => instrumentById[id]).filter(Boolean).sort((left, right) => String(legalDateValue(left)).localeCompare(String(legalDateValue(right))))})).filter(family => family.items.length);
  const groupLabel = key => LEGAL_CLASS_LABELS_TH[key] || (key === "current" ? "ฉบับที่ใช้งานอยู่" : key === "historical" ? "ฉบับก่อนหน้า" : "ตราสารอื่น ๆ");
  return <section className="legal-map-shell">
    <div className="legal-map-header">
      <div>
        <p className="eyebrow">LEGAL MAP</p>
        <h2>Organize law by instrument</h2>
        <p className="section-copy">Start with the legal instruments, then open one document to inspect its provisions and evidence.</p>
      </div>
      <div className="preview-actions"><Button label="Advanced graph" size="sm" variant="secondary" onClick={onOpenAdvanced}/></div>
    </div>
    <div className="legal-map-stat-grid">
      <div className="legal-map-stat primary"><span>ฉบับทั้งหมด</span><strong>{instruments.length}</strong><small>ตราสารในทะเบียนกฎหมาย</small></div>
      <div className="legal-map-stat"><span>มีผลใช้บังคับ</span><strong>{current.length}</strong><small>รวมฉบับแก้ไขที่ยังมีผล</small></div>
      <div className="legal-map-stat"><span>ประวัติฉบับก่อนหน้า</span><strong>{historical.length}</strong><small>เก็บไว้สำหรับตรวจสอบย้อนหลัง</small></div>
      <div className="legal-map-stat"><span>โครงสร้างที่สกัดได้</span><strong>{nodeCount}</strong><small>{relationCount} ความสัมพันธ์ภายในเอกสาร</small></div>
    </div>
    <div className="legal-map-controls">
      <label className="legal-search-field"><span>ค้นหาตราสาร</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="ชื่อกฎหมายหรือชื่อไฟล์" aria-label="ค้นหาตราสารกฎหมาย"/></label>
      <DesignSystemSelect label="แสดง" value={statusFilter} onChange={setStatusFilter} options={[{value: "current", label: "ฉบับที่ใช้งานอยู่"}, {value: "historical", label: "ฉบับก่อนหน้า"}, {value: "all", label: "ทุกฉบับ"}]} className="legal-filter-field"/>
      <span className="legal-map-result-count">แสดง {visible.length} จาก {instruments.length} ฉบับ</span>
    </div>
    {loading && <p className="section-copy" role="status">Loading legal map…</p>}
    {!loading && !instruments.length && <div className="legal-map-empty"><b>No legal instruments are ready yet</b><span>Process a legal document first, then rebuild the legal graph.</span></div>}
    {!loading && currentInstrument && <section className="legal-current-panel"><div className="legal-current-kicker"><span className="eyebrow">CURRENT VERSION</span><span className="legal-status in_force"><i aria-hidden="true"/>มีผลใช้บังคับ</span></div><button type="button" className="legal-current-card" onClick={() => onSelectInstrument(currentInstrument.id)}><div><p className="legal-current-kind">{legalClassLabel(currentInstrument)}</p><h3>{currentInstrument.title}</h3><p>{currentInstrument.filename || currentInstrument.document_id}</p><div className="legal-current-meta"><span><b>{legalDateLabel(currentInstrument)}</b></span><span>{currentInstrument.entity_count || 0} โหนด</span><span>{currentInstrument.relationship_count || 0} ความสัมพันธ์</span></div></div><span className="legal-current-action">ดูโครงสร้างมาตรา <b>→</b></span></button></section>}
    {!loading && timelineFamilies.length > 0 && <section className="legal-timeline-primary"><div className="legal-timeline-primary-heading"><div><p className="eyebrow">LEGAL TIMELINE</p><h2>วิวัฒนาการของกฎหมาย</h2><p className="section-copy">ติดตามฉบับหลัก ฉบับแก้ไข และฉบับรวมตามลำดับเวลา เลือกแต่ละรายการเพื่อดูโครงสร้างและหลักฐาน</p></div><div className="legal-timeline-legend"><span><i className="in_force"/>มีผลใช้บังคับ</span><span><i className="superseded"/>ฉบับก่อนหน้า</span></div></div>{timelineFamilies.map(family => <div className="legal-timeline-family" key={family.id}><div className="legal-timeline-family-title"><b>{family.title}</b><span>{family.items.length} ฉบับในสายกฎหมาย</span></div><div className="legal-timeline-track legal-timeline-track-primary" style={{"--timeline-count": family.items.length}}><span className="legal-timeline-axis" aria-hidden="true"/>{family.items.map((item, index) => <button type="button" key={item.id} className={`legal-timeline-item legal-timeline-item-primary ${index % 2 === 0 ? "is-top" : "is-bottom"} ${item.id === currentInstrument?.id ? "is-current" : ""}`} onClick={() => onSelectInstrument(item.id)}><span className="legal-timeline-date">{legalDateValue(item) || "ไม่ระบุวันที่"}</span><i className={`legal-timeline-dot ${item.status || "unknown"}`} aria-hidden="true"/><span className="legal-timeline-kind">{legalClassLabel(item)}</span><strong>{item.title}</strong><small>{legalStatusLabel(item.status)}{item.version_label ? ` · ${item.version_label}` : ""}</small><em>เปิดโครงสร้าง →</em></button>)}</div></div>)}</section>}
    {!loading && visible.length > 0 && <div className="legal-instrument-sections">{Object.entries(grouped).map(([key, rows]) => <section key={key} className="legal-instrument-section"><div className="legal-instrument-section-heading"><div><p className="eyebrow">{key === "amendment" ? "AMENDMENTS" : key === "consolidated" ? "CONSOLIDATED" : "HISTORY"}</p><h3>{groupLabel(key)}</h3></div><span>{rows.length} ฉบับ</span></div><div className="legal-instrument-grid">{rows.map(renderCard)}</div></section>)}</div>}
    {!loading && !visible.length && instruments.length > 0 && <div className="legal-map-empty"><b>ไม่พบตราสารที่ตรงกับตัวกรอง</b><span>ลองเปลี่ยนคำค้นหาหรือเลือก “ทุกฉบับ”</span></div>}
  </section>;
}

function LegalGraphNavigator(props) {
  const [map, setMap] = useState(null);
  const [mapLoading, setMapLoading] = useState(true);
  const [presentation, setPresentation] = useState("map");
  const [subgraph, setSubgraph] = useState(null);
  const [subgraphLoading, setSubgraphLoading] = useState(false);
  const loadMap = useCallback(async () => {
    setMapLoading(true);
    try { setMap(await api(`/v1/knowledge-bases/${props.knowledgeBaseId}/legal-map?view=${props.legalGraphView}`)); }
    catch { setMap({instruments: []}); }
    finally { setMapLoading(false); }
  }, [props.knowledgeBaseId, props.legalGraphView]);
  useEffect(() => { setPresentation("map"); setSubgraph(null); loadMap(); }, [loadMap]);
  const openInstrument = async instrumentId => {
    setSubgraphLoading(true);
    try { setSubgraph(await api(`/v1/knowledge-bases/${props.knowledgeBaseId}/legal-map?view=${props.legalGraphView}&instrument_id=${instrumentId}&max_nodes=80`)); setPresentation("instrument"); }
    catch { setSubgraph(null); }
    finally { setSubgraphLoading(false); }
  };
  if (presentation === "map") return <LegalMapPanel map={map} loading={mapLoading} onSelectInstrument={openInstrument} onOpenAdvanced={() => setPresentation("advanced")}/>;
  if (presentation === "instrument" && subgraph) return <section className="legal-instrument-view"><div className="legal-view-header"><div><button type="button" className="legal-back-link" onClick={() => setPresentation("map")}>← Legal Map</button><p className="eyebrow">DOCUMENT STRUCTURE</p><h2>{subgraph.instrument?.title}</h2><p className="section-copy">This view is bounded to one legal instrument. Select a node to inspect evidence, versions, and related provisions.</p></div><div className="preview-actions"><Button label="Advanced graph" size="sm" variant="secondary" onClick={() => setPresentation("advanced")}/></div></div>{subgraphLoading && <p className="section-copy" role="status">Loading document structure…</p>}<GraphCanvas {...props} entities={subgraph.nodes || []} relationships={subgraph.edges || []}/></section>;
  return <section className="legal-instrument-view"><div className="legal-view-header"><div><button type="button" className="legal-back-link" onClick={() => setPresentation("map")}>← Legal Map</button><p className="eyebrow">ADVANCED GRAPH</p><h2>Advanced legal evidence graph</h2><p className="section-copy">This view is bounded by the API safety limit. Use Search, Type, Review, or select a document structure to keep the analysis focused.</p></div><div className="preview-actions"><Button label="Back to Legal Map" size="sm" variant="secondary" onClick={() => setPresentation("map")}/></div></div><GraphCanvas {...props}/></section>;
}

function GraphWorkspace(props) {
  return <ReactFlowProvider>{props.isLegalGraph ? <LegalGraphNavigator {...props}/> : <GraphCanvas {...props}/>}</ReactFlowProvider>;
}

function LegalInspector({entity, data, loading, tab, setTab, onImpact, onFocus}) {
  const legal = data?.entity || entity;
  const context = data?.context || {};
  const evidence = data?.evidence || [];
  const incoming = data?.relationships?.incoming || [];
  const outgoing = data?.relationships?.outgoing || [];
  const versions = data?.versions?.family || [];
  const warnings = data?.analysis?.warnings || [];
  const statusVariant = {verified: "success", suggested: "warning", rejected: "error"}[legal.review_status] || "neutral";
  return <div className="legal-inspector">
    <p className="eyebrow">SELECTED LEGAL ENTITY</p><h2>{legal.name}</h2>
    <div className="inspector-badges"><Badge label={legal.entity_type} variant="info"/><Badge label={legal.review_status || "unreviewed"} variant={statusVariant}/>{legal.origin && <Badge label={legal.origin.replace(/_/g, " ")} variant="neutral"/>}</div>
    <div className="inspector-tabs" role="tablist">{[["overview","Overview"],["evidence","Evidence"],["relations","Relationships"],["versions","Versions"]].map(([value,label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}</button>)}</div>
    {loading && <p className="section-copy" role="status">Loading legal context…</p>}
    {!loading && tab === "overview" && <div className="inspector-section">
      <dl className="inspector-meta"><div><dt>Identity</dt><dd><code>{legal.identity_key || legal.id}</code></dd></div><div><dt>Confidence</dt><dd>{legal.confidence == null ? "—" : `${Math.round(legal.confidence * 100)}%`}</dd></div><div><dt>Sources</dt><dd>{legal.source_count ?? evidence.length}</dd></div></dl>
      {context.documents?.map(document => <div className="inspector-context-card" key={document.document_id}><b>{document.title}</b><span>{document.document_type} · {document.status}</span>{document.instrument && <span>{document.instrument.kind} · {document.instrument.status}{document.instrument.version_label ? ` · ${document.instrument.version_label}` : ""}</span>}</div>)}
      {!context.documents?.length && <p className="section-copy">No document context is linked to this node.</p>}
      {warnings.map(warning => <p className="inline-error" key={warning}>⚠ {warning}</p>)}
      <div className="preview-actions"><Button label="Focus neighbours" size="sm" variant="secondary" onClick={() => onFocus(1)}/><Button label="Analyze impact" size="sm" variant="secondary" onClick={onImpact}/></div>
    </div>}
    {!loading && tab === "evidence" && <div className="inspector-section">{evidence.length ? evidence.map((source, index) => <details className="inspector-evidence" open={index === 0} key={`${source.document_id}-${index}`}><summary>{source.title}</summary><p>{source.excerpt || "No excerpt stored."}</p></details>) : <p className="section-copy">No supporting evidence was stored for this entity.</p>}</div>}
    {!loading && tab === "relations" && <div className="inspector-section">{[...incoming, ...outgoing].length ? <ul className="inspector-relations">{[...incoming, ...outgoing].map(relation => <li key={relation.id}><b>{relation.direction === "incoming" ? "←" : "→"} {relation.relationship_type.replace(/_/g, " ")}</b><span>{relation.other_entity?.name || "Unknown entity"} · {relation.review_status} · {relation.origin}</span>{relation.sources?.[0]?.excerpt && <small>{relation.sources[0].excerpt}</small>}</li>)}</ul> : <p className="section-copy">No verified or manual relationships are connected.</p>}</div>}
    {!loading && tab === "versions" && <div className="inspector-section">{versions.length ? versions.map(version => <div className="inspector-context-card" key={version.id}><b>{version.official_title || version.document_id}</b><span>{version.kind} · {version.status} · {version.effective_from || "date unknown"}</span></div>) : <p className="section-copy">No instrument family/version history is linked.</p>}{data?.versions?.relations?.map(relation => <p className="section-copy" key={relation.id}>{relation.relation} · {relation.review_status}{relation.evidence_quote ? ` · ${relation.evidence_quote}` : ""}</p>)}</div>}
    <p className="section-copy graph-help">Legal nodes are read-only here and are rebuilt from sourced metadata. Review suggestions before treating them as facts.</p>
  </div>;
}

function GraphCanvas({knowledgeBaseId, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship}) {
  const {screenToFlowPosition, fitView} = useReactFlow();
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selectedEntityId, setSelectedEntityId] = useState("");
  const [selectedRelationshipId, setSelectedRelationshipId] = useState("");
  const [draftPosition, setDraftPosition] = useState(null);
  const [entityName, setEntityName] = useState("");
  const [entityType, setEntityType] = useState("Application");
  const [relationshipType, setRelationshipType] = useState("DEPENDS_ON");
  const [scenario, setScenario] = useState("stops working");
  const [graphNotice, setGraphNotice] = useState("");
  const [layout, setLayout] = useState({});
  const [editName, setEditName] = useState("");
  const [editEntityType, setEditEntityType] = useState("Application");
  const [editRelationshipType, setEditRelationshipType] = useState("DEPENDS_ON");
  const [inspectorData, setInspectorData] = useState(null);
  const [inspectorLoading, setInspectorLoading] = useState(false);
  const [inspectorTab, setInspectorTab] = useState("overview");
  const [graphSearch, setGraphSearch] = useState("");
  const [graphTypeFilter, setGraphTypeFilter] = useState("all");
  const [graphStatusFilter, setGraphStatusFilter] = useState("all");
  const [graphDepth, setGraphDepth] = useState(1);

  useEffect(() => { api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`).then(data => setLayout(Object.fromEntries(data.items.map(item => [item.entity_id, {x: item.x, y: item.y}])))).catch(() => setLayout({})); }, [knowledgeBaseId]);

  useEffect(() => {
    const degree = relationships.reduce((counts, relationship) => ({...counts, [relationship.source_entity_id]: (counts[relationship.source_entity_id] || 0) + 1, [relationship.target_entity_id]: (counts[relationship.target_entity_id] || 0) + 1}), {});
    const ordered = [...entities].sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0) || a.name.localeCompare(b.name));
    const rankById = Object.fromEntries(ordered.map((entity, index) => [entity.id, index]));
    const visible = entities.filter(entity => (!graphSearch.trim() || `${entity.name} ${entity.entity_type}`.toLowerCase().includes(graphSearch.trim().toLowerCase())) && (graphTypeFilter === "all" || entity.entity_type === graphTypeFilter) && (graphStatusFilter === "all" || entity.review_status === graphStatusFilter));
    setNodes(current => visible.map((entity, index) => {
      const existing = current.find(node => node.id === entity.id);
      const rank = rankById[entity.id] ?? index;
      const ringCount = Math.max(1, entities.length - 1);
      const angle = ringCount > 1 ? ((rank - 1) / ringCount) * Math.PI * 2 - Math.PI / 2 : 0;
      const radius = Math.max(230, Math.min(360, ringCount * 48));
      const automaticPosition = rank === 0 ? {x: 520, y: 350} : {x: 520 + Math.cos(angle) * radius, y: 350 + Math.sin(angle) * radius};
      return {id: entity.id, type: "knowledge", position: existing?.position || layout[entity.id] || automaticPosition, data: {label: entity.name, entityType: entity.entity_type, documentId: entity.attributes?.document_id, reviewStatus: entity.review_status}};
    }));
  }, [entities, relationships, layout, setNodes, graphSearch, graphTypeFilter, graphStatusFilter]);

  useEffect(() => {
    const nodesById = Object.fromEntries(nodes.map(node => [node.id, node]));
    setEdges(relationships.filter(relationship => nodesById[relationship.source_entity_id] && nodesById[relationship.target_entity_id]).map(relationship => {
      const isDependency = /DEPEND|RUNS_ON|USES/i.test(relationship.relationship_type);
      const handles = connectionHandles(nodesById[relationship.source_entity_id], nodesById[relationship.target_entity_id]);
      return {id: relationship.id, source: relationship.source_entity_id, target: relationship.target_entity_id, ...handles, label: relationship.relationship_type.replace(/_/g, " "), type: "straight", markerEnd: {type: MarkerType.ArrowClosed, color: isDependency ? "#56328d" : "#008c96"}, style: {stroke: isDependency ? "#56328d" : "#008c96", strokeWidth: 1.8}, labelStyle: {fill: isDependency ? "#387f3f" : "#15727a", fontWeight: 700, fontSize: 11}, labelBgStyle: {fill: "#ffffff", fillOpacity: 0.94}};
    }));
  }, [nodes, relationships, setEdges]);

  useEffect(() => { if (entities.length) requestAnimationFrame(() => fitView({padding: 0.3, duration: 280})); }, [entities.length, fitView]);

  const selectedEntity = entities.find(entity => entity.id === selectedEntityId);
  const selectedRelationship = relationships.find(relationship => relationship.id === selectedRelationshipId);
  useEffect(() => { if (selectedEntity) { setEditName(selectedEntity.name); setEditEntityType(selectedEntity.entity_type); } }, [selectedEntity]);
  useEffect(() => { if (selectedRelationship) setEditRelationshipType(selectedRelationship.relationship_type); }, [selectedRelationship]);
  useEffect(() => {
    if (!selectedEntity || !isLegalGraph) { setInspectorData(null); return undefined; }
    let active = true; setInspectorLoading(true); setInspectorTab("overview");
    api(`/v1/entities/${selectedEntity.id}/inspector?depth=${graphDepth}`).then(data => { if (active) setInspectorData(data); }).catch(() => { if (active) setInspectorData({entity: selectedEntity, analysis: {warnings: ["Unable to load detailed legal context."]}}); }).finally(() => { if (active) setInspectorLoading(false); });
    return () => { active = false; };
  }, [selectedEntityId, isLegalGraph, graphDepth]);
  const focusSelected = async depth => {
    setGraphDepth(depth); if (!selectedEntityId) return;
    const neighbourhood = await api(`/v1/entities/${selectedEntityId}/graph?depth=${depth}`).catch(() => null);
    if (!neighbourhood) return;
    const ids = new Set(neighbourhood.nodes.map(node => node.id)); setNodes(current => current.map(node => ({...node, hidden: !ids.has(node.id)}))); setEdges(current => current.map(edge => ({...edge, hidden: !ids.has(edge.source) || !ids.has(edge.target)}))); setGraphNotice(`Showing ${ids.size} connected nodes at depth ${depth}.`);
  };
  const onPaneClick = useCallback(event => {
    if (isLegalGraph && legalGraphView !== "manual") {
      setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(null);
      setGraphNotice("Switch to Manual graph to add an editor-created entity. Verified legal facts are rebuilt from sourced metadata.");
      return;
    }
    const point = screenToFlowPosition({x: event.clientX, y: event.clientY});
    setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(point); setEntityName(""); setGraphNotice("Name the new entity in the panel, then add it to this position.");
  }, [screenToFlowPosition, isLegalGraph, legalGraphView]);
  const onNodeClick = useCallback((_, node) => { setSelectedEntityId(node.id); setSelectedRelationshipId(""); setDraftPosition(null); setGraphNotice(""); }, []);
  const onConnect = useCallback(async connection => {
    if (isLegalGraph && legalGraphView !== "manual") { setGraphNotice("Switch to Manual graph before creating an editor relationship."); return; }
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const duplicate = relationships.some(item => item.source_entity_id === connection.source && item.target_entity_id === connection.target && item.relationship_type === relationshipType);
    if (duplicate) { setGraphNotice("This relationship already exists."); return; }
    const created = await addRelationship({sourceEntityId: connection.source, targetEntityId: connection.target, relationshipType});
    if (created) setGraphNotice(`Connection created: ${relationshipType.replace(/_/g, " ")}.`);
  }, [addRelationship, relationshipType, relationships, isLegalGraph, legalGraphView]);
  const createNode = async event => {
    event.preventDefault(); const created = await addEntity({name: entityName, entityType});
    if (created) {
      const position = draftPosition || {x: 80, y: 80};
      setLayout(current => ({...current, [created.id]: position}));
      await api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`, {method: "PUT", body: JSON.stringify({items: [{entity_id: created.id, x: position.x, y: position.y}]})});
      setDraftPosition(null); setSelectedEntityId(created.id); setGraphNotice("Entity added. Drag its right handle to connect it.");
    }
  };
  const runImpactForSelected = async event => {
    event.preventDefault(); if (!selectedEntity) return; await analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario});
  };
  const saveLayout = useCallback(async nextNodes => {
    try { await api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`, {method: "PUT", body: JSON.stringify({items: nextNodes.map(node => ({entity_id: node.id, x: node.position.x, y: node.position.y}))})}); }
    catch { setGraphNotice("The graph arrangement could not be saved."); }
  }, [knowledgeBaseId]);
  const onNodeDragStop = useCallback((_, __, nextNodes) => saveLayout(nextNodes?.length ? nextNodes : nodes), [nodes, saveLayout]);
  const updateSelectedEntity = async event => {
    event.preventDefault(); if (!selectedEntity || !editName.trim()) return;
    await api(`/v1/entities/${selectedEntity.id}`, {method: "PATCH", body: JSON.stringify({name: editName.trim(), entity_type: editEntityType})});
    await refreshGraph(); setGraphNotice("Entity details saved.");
  };
  const deleteSelectedEntity = async () => {
    if (!selectedEntity || !window.confirm(`Delete ${selectedEntity.name} and its relationships?`)) return;
    await api(`/v1/entities/${selectedEntity.id}`, {method: "DELETE"}); setSelectedEntityId(""); await refreshGraph(); setGraphNotice("Entity and its relationships deleted.");
  };
  const selectEdge = (_, edge) => { setSelectedRelationshipId(edge.id); setSelectedEntityId(""); setDraftPosition(null); setGraphNotice(""); };
  const updateSelectedRelationship = async event => {
    event.preventDefault(); if (!selectedRelationship) return;
    await api(`/v1/relationships/${selectedRelationship.id}`, {method: "PATCH", body: JSON.stringify({relationship_type: editRelationshipType})});
    await refreshGraph(); setGraphNotice("Relationship details saved.");
  };
  const deleteSelectedRelationship = async () => {
    if (!selectedRelationship || !window.confirm(`Delete ${selectedRelationship.relationship_type.replace(/_/g, " ")} relationship?`)) return;
    await api(`/v1/relationships/${selectedRelationship.id}`, {method: "DELETE"}); setSelectedRelationshipId(""); await refreshGraph(); setGraphNotice("Relationship deleted.");
  };
  const syncGraph = async () => {
    const result = await syncGraphFromDocuments();
    if (result) setGraphNotice(result.entities || result.relationships ? "Imported graph evidence from processed documents." : "No new graph evidence was found. Try reprocessing a completed document.");
  };

  const closeInspector = () => {
    setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(null); setGraphNotice("");
  };
  const isInspectorOpen = Boolean(draftPosition || selectedEntity || selectedRelationship || graphNotice);
  const legalTypes = [...new Set(entities.filter(entity => entity.is_legal).map(entity => entity.entity_type))];
  const selectedEntityPanel = isLegalGraph && legalGraphView !== "manual" ? <LegalInspector entity={selectedEntity} data={inspectorData} loading={inspectorLoading} tab={inspectorTab} setTab={setInspectorTab} onImpact={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})} onFocus={focusSelected}/> : <form className="form-stack" onSubmit={updateSelectedEntity}><p className="eyebrow">SELECTED ENTITY</p><h2>Edit entity</h2><TextInput label="Entity name" value={editName} onChange={setEditName} isRequired/><DesignSystemSelect label="Type" value={editEntityType} onChange={setEditEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: type}))} size="md"/><p className="section-copy graph-help">Drag from any edge of this node to another node to create a {relationshipType.replace(/_/g, " ")} relationship.</p><Button label="Save entity" type="submit" variant="primary" isDisabled={!editName.trim()}/><div className="form-stack graph-impact-form"><TextInput label="Impact scenario" value={scenario} onChange={setScenario} placeholder="e.g. stops working" isRequired/><Button label="Analyze impact" type="button" variant="secondary" onClick={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})}/></div><Button label="Delete entity" type="button" variant="destructive" onClick={deleteSelectedEntity}/></form>;
  return <section className="graph-workspace"><div className="graph-toolbar"><div><Badge label={`${entities.length} entities`} variant="info"/><Badge label={`${relationships.length} relationships`} variant="neutral"/></div>{isLegalGraph ? <><DesignSystemSelect label="Graph view" value={legalGraphView} onChange={setLegalGraphView} options={[{value: "verified", label: "Verified legal structure"}, {value: "suggested", label: "Suggested relationships"}, {value: "manual", label: "Manual graph"}, {value: "all", label: "All graph evidence"}]}/><label className="relationship-picker">Search<input value={graphSearch} onChange={event => setGraphSearch(event.target.value)} placeholder="Entity name"/></label><DesignSystemSelect label="Type" value={graphTypeFilter} onChange={setGraphTypeFilter} options={[{value: "all", label: "All types"}, ...legalTypes.map(type => ({value: type, label: type}))]}/><DesignSystemSelect label="Review" value={graphStatusFilter} onChange={setGraphStatusFilter} options={[{value: "all", label: "All statuses"}, {value: "verified", label: "Verified"}, {value: "suggested", label: "Suggested"}, {value: "rejected", label: "Rejected"}]}/><Button label={legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status) ? "Rebuilding legal graph…" : "Rebuild legal graph"} variant="secondary" size="sm" onClick={queueLegalGraphRebuild} isDisabled={Boolean(legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status))}/></> : <><DesignSystemSelect label="New connection type" value={relationshipType} onChange={setRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: type}))}/><Button label="Import from documents" variant="secondary" size="sm" onClick={syncGraph}/></>}<Button label="Fit graph" variant="ghost" size="sm" onClick={() => fitView({padding: 0.24, duration: 280})}/></div>
    <div className="graph-layout"><div className="graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={graphNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={onPaneClick} onNodeDragStop={onNodeDragStop} onEdgeClick={selectEdge} onConnect={onConnect} fitView fitViewOptions={{padding: 0.3}} minZoom={0.25} maxZoom={2} nodesConnectable connectionMode="loose" connectionRadius={24} defaultEdgeOptions={{type: "smoothstep"}}><Background gap={20} size={1} color="#b9cbd3"/><MiniMap pannable zoomable nodeColor="#2c7282"/><Controls showInteractive={false}/></ReactFlow></div>
      <aside className={`graph-inspector ${isInspectorOpen ? "open" : "closed"}`}>{isInspectorOpen && <button type="button" className="graph-inspector-close" onClick={closeInspector} aria-label="Close inspector" style={{position: "absolute", top: 12, right: 14, border: 0, background: "transparent", color: "#52717a", fontSize: "1.5rem", lineHeight: 1, cursor: "pointer"}}>×</button>}{draftPosition ? <form className="form-stack" onSubmit={createNode}><p className="eyebrow">NEW ENTITY</p><h2>Add to graph</h2><p className="section-copy">This entity will be placed where you clicked.</p><TextInput label="Entity name" value={entityName} onChange={setEntityName} placeholder="e.g. Payment API" isRequired hasAutoFocus/><DesignSystemSelect label="Type" value={entityType} onChange={setEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: type}))} size="md"/><Button label="Add entity" type="submit" variant="primary" isDisabled={!entityName.trim()}/></form> : selectedEntity ? selectedEntityPanel : selectedRelationship ? <form className="form-stack" onSubmit={updateSelectedRelationship}><p className="eyebrow">{selectedRelationship.review_status === "suggested" ? "SUGGESTED LEGAL RELATIONSHIP" : "SELECTED RELATIONSHIP"}</p><h2>{selectedRelationship.relationship_type.replace(/_/g, " ")}</h2><p className="section-copy">{selectedRelationship.origin.replace(/_/g, " ")} · {selectedRelationship.review_status}</p>{selectedRelationship.sources?.length ? <div className="legal-evidence"><b>Evidence</b>{selectedRelationship.sources.map(source => <details key={`${source.document_id}-${source.excerpt}`}><summary>{source.title}</summary><p>{source.excerpt}</p></details>)}</div> : <p className="section-copy">No supporting excerpt was stored for this relationship.</p>}{selectedRelationship.origin === "ai_suggestion" && selectedRelationship.review_status === "suggested" ? <div className="preview-actions"><Button label="Approve" type="button" variant="primary" onClick={() => reviewLegalRelationship(selectedRelationship.id, "verified")}/><Button label="Reject" type="button" variant="destructive" onClick={() => reviewLegalRelationship(selectedRelationship.id, "rejected")}/></div> : (!isLegalGraph || legalGraphView === "manual") && <><DesignSystemSelect label="Relationship type" value={editRelationshipType} onChange={setEditRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: type}))} size="md"/><Button label="Save connection" type="submit" variant="primary"/><Button label="Delete connection" type="button" variant="destructive" onClick={deleteSelectedRelationship}/></>}</form> : null}{graphNotice && <p className="graph-notice" role="status">{graphNotice}</p>}</aside></div>
    {impact && <Impact data={impact}/>} 
  </section>;
}

function buildAgentSkillRules() {
  return `# Softnix Knowledge Base — grounded answering

You are connected to one or more Knowledge Bases through the
\`softnix-knowledge\` MCP server. The MCP Bearer token is the source of truth for
which active Knowledge Bases and tools this Agent may access. Do not assume a
fixed Knowledge Base name or subject area. Answer the user **only** from
evidence returned by this MCP server.

## Absolute rule — one source of truth

For every factual claim in your answer:

- You MUST obtain it by calling a \`softnix-knowledge\` MCP tool listed below.
- You MUST NOT use any other source, including:
  - web search / internet search tools
  - web fetch, URL fetching, or page browsing
  - other MCP servers, file readers, or databases
  - your own training data or prior knowledge
- Do NOT blend outside information into the answer. The user must receive an
  answer that comes entirely from this knowledge base and is traceable to its
  citations — never a mix of this source and anything else.
- Treat the MCP token's authorized Knowledge Base scope as authoritative. Do not
  broaden it by sending or changing \`knowledge_base_ids\` in a request.
- If the knowledge base does not contain the answer (a tool returns
  \`insufficient_evidence: true\` or no \`sources\`), say so plainly, for example:
  "ไม่พบข้อมูลนี้ในฐานความรู้ที่เชื่อมต่ออยู่ (Softnix Knowledge Base)."
  Then stop or ask the user to rephrase — do NOT fill the gap from any other
  source.
- If a question is outside the scope above, tell the user it is out of scope
  instead of answering it from elsewhere.

## Tools (softnix-knowledge MCP server)

- \`search_knowledge\` — primary tool. Send the user's question; it returns a
  grounded answer, cited \`sources\` ([S1], [S2], …), and metadata. Use it for
  almost every question, before you write anything.
- \`find_entities\` — look up entities by name or alias.
- \`analyze_relationships\` — how specific entities relate to each other.
- \`analyze_impact\` — direct and indirect impact of a change or failure.
- \`get_sources\` — fetch full source excerpts for a prior \`result_id\`.
- \`resolve_legal_context\` — resolve the current legal instrument/provision context before answering legal questions.
- \`get_legal_instrument\` — inspect an instrument's family, provenance, status, and reviewed relations.
- \`get_provision_history\` — compare document-scoped versions of a provision without merging same-number provisions.

Optional \`search_knowledge\` filters, when relevant:

- \`as_of_date\` (YYYY-MM-DD) — for legal or time-sensitive questions, restrict to
  the version in force on that date.
- \`include_historical\` (true/false) — include superseded or repealed versions
  only when the user explicitly asks about past versions.

## Workflow

1. If the question needs facts, call \`search_knowledge\` FIRST — before writing
   any part of the answer.
2. Base the answer strictly on the returned \`answer\` and \`sources\`. Keep the
   [S#] citations so the user can verify every claim.
3. If sources carry legal status or version metadata (e.g. สถานะ: บังคับใช้ /
   ถูกยกเลิก, effective date, version), respect it: prefer text that is in force,
   state which version and date you relied on, and surface any \`warnings\`
   (such as superseded or repealed provisions) to the user.
4. If \`insufficient_evidence\` is true or there are no sources, tell the user the
   information is not in the knowledge base and offer to refine the query. Do
   not answer from memory or the web.
5. For entity, relationship, or impact questions, use \`find_entities\`,
   \`analyze_relationships\`, or \`analyze_impact\` instead of guessing.
6. For legal questions, call \`resolve_legal_context\` first when a provision,
   amendment, effective date, or instrument is mentioned. Treat unresolved or
   suggested relations as leads, never as verified facts.

## Answering style

- Reply in the user's language (Thai or English), but every fact must come from
  retrieved sources.
- Always show the citations that the tools returned.
- Never fabricate document names, numbers, dates, or quotations. If unsure,
  retrieve again or state that it is not available.
- Do not mention any source that the tools did not return.
`;
}

function buildAgentSkillMd() {
  return `---
name: softnix-knowledge
description: >-
  Answer questions using ONLY the Knowledge Bases authorized by the active
  Softnix Knowledge MCP token. Use this whenever the user asks about information
  that may be available through the connected Softnix Knowledge MCP server.
  Never answer from web search, web fetch, browsing, other tools, or training
  knowledge.
---

${buildAgentSkillRules()}`;
}

function AccessView({selectedKb, knowledgeBases, tokens, auditLogs, mcpActivity, loadAccess, createMcpToken, rotateMcpToken, changeTokenState}) {
  const allTools = ["search_knowledge", "find_entities", "analyze_relationships", "analyze_impact", "get_sources", "resolve_legal_context", "get_legal_instrument", "get_provision_history"];
  const activeKnowledgeBases = knowledgeBases.filter(kb => kb.status === "active");
  const [name, setName] = useState(""); const [secret, setSecret] = useState(""); const [isLoading, setIsLoading] = useState(false); const [formError, setFormError] = useState(""); const [copied, setCopied] = useState("");
  const [selectedKbs, setSelectedKbs] = useState(selectedKb ? [selectedKb.id] : []); const [tools, setTools] = useState(allTools); const [expiresAt, setExpiresAt] = useState(""); const [rpm, setRpm] = useState(60); const [concurrency, setConcurrency] = useState(5); const [timeout, setTimeoutValue] = useState(60); const [operations, setOperations] = useState(null);
  const mcpUrl = `${window.location.origin}/mcp`; const tokenForGuide = secret || "YOUR_SOFTNIX_MCP_TOKEN";
  const cliCommand = `claude mcp add --transport http softnix-knowledge \"${mcpUrl}\" --header \"Authorization: Bearer ${tokenForGuide}\"`;
  const jsonConfig = JSON.stringify({mcpServers: {"softnix-knowledge": {type: "http", url: mcpUrl, headers: {Authorization: "Bearer ${SOFTNIX_MCP_TOKEN}"}}}}, null, 2);
  const skillContent = buildAgentSkillMd();
  const toggle = (value, current, setCurrent) => setCurrent(current.includes(value) ? current.filter(item => item !== value) : [...current, value]);
  const copy = async (value, label) => { await navigator.clipboard.writeText(value); setCopied(label); setTimeout(() => setCopied(""), 1800); };
  const rotate = async token => {
    if (!window.confirm(`Rotate key for ${token.name}? The current key will be revoked immediately.`)) return;
    try { const result = await rotateMcpToken(token.id); setSecret(result.token); await copy(result.token, "token"); }
    catch (error) { setFormError(error.message); }
  };
  const loadOperations = async () => { const [ready, projection] = await Promise.all([api("/v1/system/status"), api("/v1/system/graph-projection")]); setOperations({ready, projection}); };
  useEffect(() => { loadAccess().catch(() => undefined); loadOperations().catch(() => undefined); }, []);
  useEffect(() => {
    const activeIds = new Set(activeKnowledgeBases.map(kb => kb.id));
    setSelectedKbs(current => {
      const retained = current.filter(id => activeIds.has(id));
      if (retained.length || !selectedKb || !activeIds.has(selectedKb.id)) return retained;
      return [selectedKb.id];
    });
  }, [selectedKb, knowledgeBases]);
  const create = async event => { event.preventDefault(); setIsLoading(true); setFormError(""); try { const result = await createMcpToken({name, allowed_knowledge_base_ids: selectedKbs, allowed_tools: tools, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null, requests_per_minute: Number(rpm), max_concurrent_requests: Number(concurrency), query_timeout_seconds: Number(timeout)}); setSecret(result.token); setName(""); } catch (error) { setFormError(error.message); } finally { setIsLoading(false); } };
  return <><PageHeading eyebrow="ACCESS & MCP" title="Connect knowledge safely" description="Create a scoped token, copy a ready-to-run configuration, then verify the connection." actions={<Button label="Refresh status" variant="ghost" onClick={() => { loadAccess(); loadOperations(); }}/>}/><section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operations?.ready?.status || "Checking system"}</b><span>{operations ? `${Object.keys(operations.ready.dependencies || {}).length} dependencies online` : "Loading dependencies"}</span></div></div><div className="mcp-endpoint"><span>Server endpoint</span><code>{mcpUrl}</code><button type="button" onClick={() => copy(mcpUrl, "endpoint")}>Copy</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">STEP 1</p><h2>Create a scoped token</h2></div><Badge label="Secret shown once" variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label="Token name" value={name} onChange={setName} placeholder="e.g. claude-code-architecture" isRequired/><div className="scope-section"><div className="scope-heading"><b>Knowledge Base access</b>{activeKnowledgeBases.length > 0 && <button type="button" onClick={() => setSelectedKbs(activeKnowledgeBases.map(kb => kb.id))}>Select all</button>}</div><p className="section-copy">Only active Knowledge Bases can be granted to an MCP token.</p><div className="scope-options">{activeKnowledgeBases.length ? activeKnowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${selectedKbs.includes(kb.id) ? "selected" : ""}`}><input type="checkbox" checked={selectedKbs.includes(kb.id)} onChange={() => toggle(kb.id, selectedKbs, setSelectedKbs)}/><span>{kb.name}</span></label>) : <p className="section-copy">No active Knowledge Bases. Activate one from Knowledge Bases before creating a token.</p>}</div></div><div className="scope-section"><div className="scope-heading"><b>Allowed tools</b><button type="button" onClick={() => setTools(allTools)}>Enable all</button></div><div className="tool-options">{allTools.map(tool => <label key={tool} className={`tool-option ${tools.includes(tool) ? "selected" : ""}`}><input type="checkbox" checked={tools.includes(tool)} onChange={() => toggle(tool, tools, setTools)}/><span>{tool.replace(/_/g, " ")}</span></label>)}</div></div><details className="advanced-options"><summary>Advanced limits</summary><div className="limit-grid"><label>Expiry (optional)<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>Requests/min<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>Concurrent requests<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>Timeout (seconds)<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error">{formError}</p>}<Button label="Create MCP token" type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !tools.length || !selectedKbs.length}/></form></Card><Card padding={4}><p className="eyebrow">STEP 2</p><h2>Connect with Claude Code</h2><p className="section-copy">Run this command on the machine where Claude Code is installed. Use a HTTPS URL for access outside this computer.</p><div className="code-panel"><div className="code-panel-top"><b>Terminal</b><button type="button" onClick={() => copy(cliCommand, "claude command")}>{copied === "claude command" ? "Copied" : "Copy command"}</button></div><pre>{cliCommand}</pre></div><ol className="mcp-steps"><li>Create the token in Step 1 and copy it immediately.</li><li>Paste the command into Terminal.</li><li>Restart Claude Code, then run <code>/mcp</code> to confirm <code>softnix-knowledge</code> is connected.</li></ol><details className="json-config"><summary>Prefer a project <code>.mcp.json</code> file?</summary><p>Store the token in <code>SOFTNIX_MCP_TOKEN</code>, not in source control.</p><div className="code-panel"><div className="code-panel-top"><b>.mcp.json</b><button type="button" onClick={() => copy(jsonConfig, "json config")}>{copied === "json config" ? "Copied" : "Copy JSON"}</button></div><pre>{jsonConfig}</pre></div></details><details className="json-config skill-config"><summary>Add an agent Skill <Badge label="recommended" variant="success"/></summary><p>Make any connected agent answer <b>only</b> from the Knowledge Bases authorized by this MCP token and never from web search, browsing, or its own training data — so users never get answers silently mixed from other sources.</p><p className="section-copy">Follows the open <a href="https://agentskills.io" target="_blank" rel="noreferrer">agentskills.io</a> Agent Skill standard, so it works with Claude Code and any other compatible agent tool (Cursor, Gemini CLI, VS Code, GitHub Copilot, and more). Save this as <code>SKILL.md</code> inside a folder named exactly <code>softnix-knowledge</code> in your agent's skills directory — for Claude Code that is <code>.claude/skills/softnix-knowledge/SKILL.md</code>.</p><div className="code-panel"><div className="code-panel-top"><b>SKILL.md</b><button type="button" onClick={() => copy(skillContent, "skill")}>{copied === "skill" ? "Copied" : "Copy SKILL"}</button></div><pre className="skill-preview">{skillContent}</pre></div></details>{secret && <div className="token-reveal"><b>New token — copy it now</b><code>{secret}</code><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? "Copied" : "Copy token"}</button></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">ACTIVE ACCESS</p><h2>Tokens</h2></div><span className="section-copy">Revoke a token immediately if a machine or credential is no longer trusted.</span></div>{tokens.length ? <div className="token-list">{tokens.map(token => <article className="token-row" key={token.id}><div><b>{token.name}</b><p>{token.token_prefix}… · {token.allowed_tools.length} tools · {token.allowed_knowledge_base_ids.length} knowledge bases</p><small>{token.requests_per_minute}/min · {token.max_concurrent_requests} concurrent · {token.query_timeout_seconds}s timeout{token.expires_at ? ` · expires ${new Date(token.expires_at).toLocaleString()}` : ""}</small></div><StatusBadge status={token.status}/><div className="document-actions">{token.status !== "revoked" && <Button label="Rotate key" size="sm" variant="secondary" onClick={() => rotate(token)}/>}{token.status === "active" && <Button label="Disable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "disable")}/>} {token.status === "inactive" && <Button label="Enable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "enable")}/>} {token.status !== "revoked" && <Button label="Revoke" size="sm" variant="destructive" onClick={() => changeTokenState(token.id, "revoke")}/>}</div></article>)}</div> : <EmptyState title="No MCP tokens yet" description="Create a token above to connect Claude Code or another MCP client."/>}</section><section className="content-section"><h2>Recent audit activity</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || "system"} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title="No activity yet" description="Administrative actions will appear here."/>}</section></>;
}

function McpActivity({activity}) {
  const [expandedId, setExpandedId] = useState(null);
  return <section className="content-section mcp-activity">
    <div className="section-title"><div><p className="eyebrow">MCP OBSERVABILITY</p><h2>Agent queries and retrieval path</h2></div><span className="section-copy">Shows the systems actually called for each MCP tool request. Token secrets and headers are never stored.</span></div>
    <div className="mcp-route-note"><b>Neo4j note:</b> it receives graph projections for exploration; current MCP retrieval uses PostgreSQL graph tables, so Neo4j appears only when a runtime path calls it.</div>
    {activity.length ? <div className="mcp-activity-list">{activity.map(row => {
      const metadata = row.metadata || {}; const isOpen = expandedId === row.id; const route = metadata.route || [];
      return <article className={`mcp-activity-row ${row.action === "mcp.tool.error" ? "has-error" : ""}`} key={row.id}>
        <button type="button" className="mcp-activity-summary" onClick={() => setExpandedId(isOpen ? null : row.id)} aria-expanded={isOpen}>
          <span><b>{metadata.tool || "MCP request"}</b><small>{metadata.token_name || "Scoped token"} · {new Date(row.created_at).toLocaleString()} · {metadata.duration_ms ?? 0} ms</small></span>
          <span className={`mcp-route-status ${row.action === "mcp.tool.error" ? "error" : ""}`}>{row.action === "mcp.tool.error" ? metadata.error_code || "failed" : `${route.filter(step => step.status === "used").length} route(s) used`}</span>
        </button>
        {isOpen && <div className="mcp-activity-detail">
          {metadata.retrieval_plan && <div><b>Planner decision</b><p>{metadata.retrieval_plan.intent} · {metadata.retrieval_plan.planner_source} · channels: {(metadata.retrieval_plan.channels || []).join(", ") || "none"}{metadata.retrieval_plan.fallback_reason ? ` · fallback: ${metadata.retrieval_plan.fallback_reason}` : ""}</p></div>}
          {metadata.query && <div><b>Agent query</b><p>{metadata.query}{metadata.query_truncated ? "…" : ""}</p></div>}
          {metadata.subjects?.length ? <div><b>Subjects</b><p>{metadata.subjects.join(", ")}</p></div> : null}
          <div><b>Retrieval path</b>{route.length ? <ol className="mcp-route-list">{route.map((step, index) => <li key={`${row.id}-${step.channel}-${index}`}><span className={`mcp-step-dot ${step.status}`}/><div><strong>{step.system}</strong><small>{step.channel.replace(/_/g, " ")} · {step.status} · {step.result_count ?? 0} result(s) · {step.duration_ms ?? 0} ms{step.detail ? ` · ${step.detail}` : ""}</small></div></li>)}</ol> : <p>No retrieval store was reached before this request was rejected.</p>}</div>
        </div>}
      </article>;
    })}</div> : <EmptyState isCompact title="No MCP tool calls yet" description="When an agent calls a knowledge tool, its query and the actual retrieval path will appear here."/>}
  </section>;
}

const Impact = ({data}) => <div className="result-panel"><h3>{data.insufficient_evidence ? "Insufficient evidence" : `Impact for ${data.subject.name}`}</h3>{data.insufficient_evidence ? <p>Upload more source material or add verified relationships before making a decision.</p> : <><h4>Direct impact</h4><ul>{data.direct_impacts.map(item => <li key={item.entity_id}>{item.name} <Badge label={item.relationship} variant="warning"/> {item.citation_ids.join(" ")}</li>)}</ul><h4>Indirect impact</h4><ul>{data.indirect_impacts.map(item => <li key={item.entity_id}>{item.path.join(" → ")} {item.citation_ids.join(" ")}</li>)}</ul></>}</div>;
const Graph = ({data}) => <div className="result-panel"><div className="graph-summary"><Badge label={`${data.nodes.length} nodes`} variant="info"/><Badge label={`${data.edges.length} connections`} variant="neutral"/></div><ul className="graph-list">{data.edges.map(edge => <li key={edge.id}><b>{data.nodes.find(node => node.id === edge.source)?.name}</b><span>{edge.type.replace(/_/g, " ")}</span><b>{data.nodes.find(node => node.id === edge.target)?.name}</b></li>)}</ul></div>;
const LEGAL_STATUS_VARIANTS = {in_force: "success", amended: "warning", not_yet_effective: "neutral", unknown: "neutral", superseded: "error", repealed: "error"};
const LegalStatusBadge = ({status}) => status ? <Badge label={status.replace(/_/g, " ")} variant={LEGAL_STATUS_VARIANTS[status] || "neutral"}/> : null;

const QueryResult = ({data, submitFeedback, onOpenSource}) => <section className="query-result"><Card padding={4}><p className="eyebrow">ANSWER</p><div className="answer-copy">{data.answer}</div>{data.warnings?.length > 0 && <div className="legal-warning-list" role="alert">{data.warnings.map((warning, index) => <p key={`${warning.code}-${index}`} className="inline-error">⚠ {warning.detail}</p>)}</div>}<div className="feedback-actions"><span>Was this result useful?</span><Button label="Yes" size="sm" variant="secondary" onClick={() => submitFeedback(data.result_id, 1)}/><Button label="No" size="sm" variant="ghost" onClick={() => submitFeedback(data.result_id, -1)}/></div>{data.metadata?.retrieval_plan && <details className="retrieval-trace"><summary>How this answer was retrieved</summary><p>{data.metadata.retrieval_plan.intent} · {data.metadata.retrieval_plan.planner_source} · {(data.metadata.retrieval_plan.channels || []).join(", ") || "no channels selected"}{data.metadata.retrieval_plan.legal_context ? ` · legal registry: ${data.metadata.retrieval_plan.legal_context.current_version_ids?.length || 0} current version(s), ${data.metadata.retrieval_plan.legal_context.excluded_document_ids?.length || 0} excluded` : ""}</p><ul>{(data.metadata.retrieval_trace || []).map((step, index) => <li key={`${step.channel}-${index}`}><b>{step.system}</b><span>{step.status} · {step.result_count ?? 0} result(s) · {step.duration_ms ?? 0} ms</span></li>)}</ul></details>}</Card><div className="sources-heading"><h2>Sources</h2><p>Every claim should be checked against its supporting excerpt.</p></div><div className="source-grid">{data.sources.map(source => <Card key={source.citation_id} padding={3}><div className="source-card-heading"><Badge label={source.citation_id} variant="info"/><LegalStatusBadge status={source.document_status}/></div><h3>{source.title}</h3>{source.section_label && <p className="section-copy">{source.section_label}{source.version_label ? ` · ${source.version_label}` : ""}{source.effective_from ? ` · มีผล ${source.effective_from}` : ""}</p>}<p>{source.excerpt}</p><Button label="Open source" size="sm" variant="ghost" onClick={() => onOpenSource({id: source.document_id, title: source.title})}/></Card>)}</div></section>;
function LegalInstrumentCard({instrument, onUpdate}) {
  const [editing, setEditing] = useState(false);
  return <div className="legal-instrument-card"><div className="legal-instrument-heading"><div><p className="eyebrow">LEGAL INSTRUMENT</p><h3>{instrument.official_title}</h3><p className="section-copy">{LEGAL_KIND_LABELS_TH[instrument.kind] || instrument.kind} · authority {instrument.authority_level}{instrument.version_label ? ` · ${instrument.version_label}` : ""}</p></div><LegalStatusBadge status={instrument.status}/></div>
    <dl className="legal-instrument-meta"><div><dt>Effective from</dt><dd>{instrument.effective_from || "—"}</dd></div><div><dt>Effective to</dt><dd>{instrument.effective_to || "—"}</dd></div><div><dt>Status source</dt><dd>{instrument.status_source}</dd></div></dl>
    {instrument.status_reason && <p className="section-copy">{instrument.status_reason}</p>}
    <Button label={editing ? "Cancel" : "Override status"} variant="ghost" size="sm" onClick={() => setEditing(value => !value)}/>
    {editing && <LegalInstrumentOverrideForm row={instrument} onSave={payload => { onUpdate(instrument.id, payload); setEditing(false); }}/>}
  </div>;
}

function DocumentPreview({preview, jobs, legalInstrument, onExtractLegal, onSaveLegal, onDeleteLegal, onUpdateLegalInstrument}) {
  const [editingLegal, setEditingLegal] = useState(false);
  const [legalDraft, setLegalDraft] = useState("");
  const [legalError, setLegalError] = useState("");
  const hasLegalMetadata = Boolean(preview.legal_metadata && Object.keys(preview.legal_metadata).length);
  useEffect(() => { setEditingLegal(false); setLegalError(""); setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); }, [preview.document_id, preview.legal_metadata]);
  const startEditing = () => { setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); setLegalError(""); setEditingLegal(true); };
  const save = async event => {
    event.preventDefault();
    try {
      const parsed = JSON.parse(legalDraft);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Metadata must be a JSON object.");
      await onSaveLegal({id: preview.document_id, title: preview.title}, parsed); setEditingLegal(false); setLegalError("");
    } catch (error) { setLegalError(error instanceof SyntaxError ? "Use valid JSON before saving." : error.message); }
  };
  return <section className="preview-section"><Card padding={4}><div className="preview-heading"><div><p className="eyebrow">DOCUMENT PREVIEW</p><h2>{preview.title}</h2></div><div className="preview-actions"><StatusBadge status={preview.status}/>{preview.status === "completed" && <Button label="Extract legal metadata" size="sm" variant="secondary" onClick={() => onExtractLegal({id: preview.document_id, title: preview.title})}/>}</div></div>
    {legalInstrument && <LegalInstrumentCard instrument={legalInstrument} onUpdate={onUpdateLegalInstrument}/>}
    {preview.error_code && <p className="inline-error">{preview.error_code}</p>}<pre className="excerpt">{preview.text || "Text will appear here when processing is complete."}</pre><div className="legal-metadata-panel"><div className="preview-heading"><div><h3>Legal metadata</h3><p className="section-copy">Legal Graph Schema v2 keeps the instrument, provisions, and cross-document references with evidence. Suggested links still require review.</p></div>{!editingLegal && <div className="preview-actions"><Button label={hasLegalMetadata ? "Edit metadata" : "Add metadata"} size="sm" variant="secondary" onClick={startEditing}/>{hasLegalMetadata && <Button label="Delete metadata" size="sm" variant="destructive" onClick={() => onDeleteLegal({id: preview.document_id, title: preview.title})}/>}</div>}</div>{editingLegal ? <form className="legal-editor" onSubmit={save}><textarea aria-label="Legal metadata JSON" value={legalDraft} onChange={event => setLegalDraft(event.target.value)} rows={18} spellCheck="false"/><p className="section-copy">Use <code>instrument</code>, <code>provisions</code>, and <code>references</code>; every fact needs an <code>evidence_quote</code>. Saving queues a safe graph rebuild.</p>{legalError && <p className="inline-error" role="alert">{legalError}</p>}<div className="preview-actions"><Button label="Save metadata" type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={() => setEditingLegal(false)}/></div></form> : hasLegalMetadata ? <pre className="excerpt legal-metadata">{JSON.stringify(preview.legal_metadata, null, 2)}</pre> : <p className="section-copy">ยังไม่มี legal metadata — กด Add metadata เพื่อเพิ่มเอง หรือ Extract legal metadata เพื่อสกัดจากเอกสาร</p>}</div><h3>Processing activity</h3>{jobs.length ? <div className="job-list">{jobs.map(job => <div key={job.id}><span>{job.type || "PROCESS_DOCUMENT"} · {job.stage || "queued"}{job.attempt_count ? ` · attempt ${job.attempt_count}` : ""}{job.error_code ? ` · ${job.error_code}` : ""}{job.error_message ? `: ${job.error_message}` : ""}</span><StatusBadge status={job.status}/><span>{job.progress_percent}%</span></div>)}</div> : <p className="section-copy">No processing jobs have been recorded yet.</p>}</Card></section>;
}

createRoot(document.getElementById("root")).render(<App/>);
