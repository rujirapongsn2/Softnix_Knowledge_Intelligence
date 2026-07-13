import React, {useCallback, useEffect, useMemo, useState} from "react";
import {createRoot} from "react-dom/client";
import {Theme} from "@astryxdesign/core/theme";
import {neutralTheme} from "@astryxdesign/theme-neutral/built";
import {AppShell} from "@astryxdesign/core/AppShell";
import {Badge} from "@astryxdesign/core/Badge";
import {Button} from "@astryxdesign/core/Button";
import {Card} from "@astryxdesign/core/Card";
import {EmptyState} from "@astryxdesign/core/EmptyState";
import {FileInput} from "@astryxdesign/core/FileInput";
import {ProgressBar} from "@astryxdesign/core/ProgressBar";
import {SideNav, SideNavHeading, SideNavItem, SideNavSection} from "@astryxdesign/core/SideNav";
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
  const [queryResult, setQueryResult] = useState(null);
  const [isQuerying, setIsQuerying] = useState(false);
  const [message, setMessage] = useState(null);
  const [activeView, setActiveView] = useState("overview");
  const [newKbName, setNewKbName] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadDocumentType, setUploadDocumentType] = useState("general");
  const [isUploading, setIsUploading] = useState(false);
  const [showDeletedDocuments, setShowDeletedDocuments] = useState(false);
  const [tokens, setTokens] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
  const [mcpActivity, setMcpActivity] = useState([]);
  const [transactionLogs, setTransactionLogs] = useState([]);
  const [traceLogs, setTraceLogs] = useState([]);
  const [legalGraphView, setLegalGraphView] = useState("verified");
  const [isLegalGraph, setIsLegalGraph] = useState(false);
  const [legalRebuildStatus, setLegalRebuildStatus] = useState(null);
  const selectedKb = useMemo(() => kbs.find(kb => kb.id === selectedKbId), [kbs, selectedKbId]);
  const completedDocuments = documents.filter(document => document.status === "completed").length;
  const processingDocuments = documents.filter(document => ["queued", "extracting", "indexing"].includes(document.status)).length;

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
    if (!id) { setEntities([]); setRelationships([]); setDocuments([]); setIsLegalGraph(false); return; }
    const nextDocuments = await api(`/v1/knowledge-bases/${id}/documents${includeDeleted ? "?include_deleted=true" : ""}`);
    const hasLegalDocuments = nextDocuments.some(document => ["legal", "regulation", "contract"].includes(document.document_type));
    const graphData = hasLegalDocuments
      ? await api(`/v1/knowledge-bases/${id}/legal-graph?view=${legalGraphView}`)
      : await Promise.all([api(`/v1/knowledge-bases/${id}/entities`), api(`/v1/knowledge-bases/${id}/relationships`)]);
    const [nextEntities, nextRelationships] = hasLegalDocuments ? [graphData.nodes, graphData.edges] : graphData;
    setEntities(nextEntities); setRelationships(nextRelationships); setDocuments(nextDocuments);
    setIsLegalGraph(hasLegalDocuments);
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
      const code = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "knowledge-base";
      const kb = await api("/v1/knowledge-bases", {method: "POST", body: JSON.stringify({name, code})});
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
  const runQuery = async event => {
    event.preventDefault(); if (!query.trim() || isQuerying) return;
    setIsQuerying(true);
    try { setQueryResult(await api("/v1/query", {method: "POST", body: JSON.stringify({query, knowledge_base_ids: selectedKbId ? [selectedKbId] : []})})); }
    catch (error) { showError(error); }
    finally { setIsQuerying(false); }
  };
  const analyzeImpact = async ({subject, scenario}) => {
    if (!selectedKbId || !subject?.trim() || !scenario?.trim()) return;
    try { setImpact(await api("/v1/query/impact", {method: "POST", body: JSON.stringify({subject: subject.trim(), scenario: scenario.trim(), knowledge_base_ids: [selectedKbId], max_depth: 3})})); }
    catch (error) { showError(error); }
  };
  const uploadDocument = async event => {
    event.preventDefault(); if (!selectedKbId || !uploadFile) return;
    const form = new FormData(); form.append("file", uploadFile); form.append("document_type", uploadDocumentType); if (uploadTitle.trim()) form.append("title", uploadTitle.trim());
    setIsUploading(true);
    try {
      const created = await api(`/v1/knowledge-bases/${selectedKbId}/documents`, {method: "POST", body: form});
      setUploadFile(null); setUploadTitle(""); setUploadDocumentType("general"); await loadKbData(selectedKbId); await openDocument({id: created.document_id, title: uploadTitle.trim() || uploadFile.name});
      notify(created.legal_extraction_automatic ? "Upload received. Legal metadata will be extracted automatically after the document is searchable." : "Upload received. Processing details are shown below.");
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
  const loadTransactionLogs = async () => {
    setTransactionLogs(await api("/v1/logs/transactions?limit=250"));
  };
  const loadTraceLogs = async () => {
    setTraceLogs(await api("/v1/traces?limit=250"));
  };
  useEffect(() => {
    if (user && activeView === "logs") Promise.all([loadTransactionLogs(), loadTraceLogs()]).catch(showError);
  }, [user, activeView]);
  const createMcpToken = async payload => {
    const result = await api("/v1/tokens", {method: "POST", body: JSON.stringify(payload)});
    await loadAccess(); notify("MCP token created. Copy it now; it will not be shown again."); return result;
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
    <SideNavSection title="WORKSPACE">
      <SideNavItem label="Overview" isSelected={activeView === "overview"} onClick={() => switchView("overview")}/>
      <SideNavItem label="Knowledge Bases" isSelected={activeView === "knowledge-bases"} onClick={() => switchView("knowledge-bases")}/>
      <SideNavItem label="Documents" isSelected={activeView === "documents"} onClick={() => switchView("documents")}/>
      <SideNavItem label="Search" isSelected={activeView === "search"} onClick={() => switchView("search")}/>
      <SideNavItem label="Explore graph" isSelected={activeView === "explore"} onClick={() => switchView("explore")}/>
    </SideNavSection>
    <SideNavSection title="SYSTEM"><SideNavItem label="Access & MCP" isSelected={activeView === "access"} onClick={() => switchView("access")}/><SideNavItem label="Logging" isSelected={activeView === "logs"} onClick={() => switchView("logs")}/></SideNavSection>
  </SideNav>;
  const topNav = <TopNav label="Workspace navigation" heading={<TopNavHeading heading={selectedKb?.name || "Knowledge workspace"}/>} endContent={<div className="topnav-user"><span className="status-indicator"/> {user.username}</div>}/>;

  return <Theme theme={neutralTheme}><AppShell topNav={topNav} sideNav={sideNav} mobileNav={{breakpoint: "md"}} height="auto" variant="elevated" contentPadding={4}>
    <div className="workspace" aria-live="polite">
      {message && <Toast body={message.body} type={message.type} isAutoHide={message.type !== "error"} autoHideDuration={5000} onDismiss={() => setMessage(null)}/>} 
      {activeView === "overview" && <Overview selectedKb={selectedKb} kbs={kbs} documents={documents} completedDocuments={completedDocuments} processingDocuments={processingDocuments} onViewDocuments={() => switchView("documents")} onSearch={() => switchView("search")} onCreate={() => switchView("knowledge-bases")}/>}
      {activeView === "knowledge-bases" && <KnowledgeBases kbs={kbs} selectedKbId={selectedKbId} setSelectedKbId={setSelectedKbId} newKbName={newKbName} setNewKbName={setNewKbName} createKb={createKb} manageKnowledgeBase={manageKnowledgeBase} updateRetrievalConfig={updateRetrievalConfig} onContinue={() => switchView("documents")}/>}
      {activeView === "documents" && (
        <Documents selectedKb={selectedKb} documents={documents} showDeletedDocuments={showDeletedDocuments} setShowDeletedDocuments={setShowDeletedDocuments} uploadFile={uploadFile} setUploadFile={setUploadFile} uploadTitle={uploadTitle} setUploadTitle={setUploadTitle} uploadDocumentType={uploadDocumentType} setUploadDocumentType={setUploadDocumentType} uploadDocument={uploadDocument} isUploading={isUploading} openDocument={openDocument} extractLegalMetadata={extractLegalMetadata} saveLegalMetadata={saveLegalMetadata} deleteLegalMetadata={deleteLegalMetadata} reprocessDocument={reprocessDocument} deleteDocument={deleteDocument} restoreDocument={restoreDocument} reindexEmbeddings={reindexEmbeddings} refreshDocuments={() => loadKbData(selectedKbId).catch(showError)} documentPreview={documentPreview} documentJobs={documentJobs} onCreateKb={() => switchView("knowledge-bases")} onSearch={() => switchView("search")} onExplore={() => switchView("explore")}/>
      )}
      {activeView === "search" && (
        <SearchView selectedKb={selectedKb} documents={documents} completedDocuments={completedDocuments} query={query} setQuery={setQuery} runQuery={runQuery} isQuerying={isQuerying} queryResult={queryResult} submitFeedback={submitQueryFeedback} onDocuments={() => switchView("documents")} onOpenSource={document => { switchView("documents"); openDocument(document); }}/>
      )}
      {activeView === "explore" && (
        <ExploreView selectedKb={selectedKb} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
      )}
      {activeView === "access" && <><AccessView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} mcpActivity={mcpActivity} loadAccess={loadAccess} createMcpToken={createMcpToken} changeTokenState={changeTokenState}/><McpActivity activity={mcpActivity}/></>}
      {activeView === "logs" && <LoggingView transactions={transactionLogs} traces={traceLogs} loadTransactions={loadTransactionLogs} loadTraces={loadTraceLogs}/>}
    </div>
  </AppShell></Theme>;
}

function LoggingView({transactions, traces, loadTransactions, loadTraces}) {
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
    {view === "traces" ? <TraceExplorer traces={traces}/> : <Card padding={4}><div className="log-filter-bar"><TextInput label="Find a request" value={search} onChange={setSearch} placeholder="Request ID, route, or authentication"/><label className="native-field">Method<select value={method} onChange={event => setMethod(event.target.value)}><option value="all">All methods</option>{methods.map(value => <option key={value} value={value}>{value}</option>)}</select></label><label className="native-field">Status<select value={status} onChange={event => setStatus(event.target.value)}><option value="all">All statuses</option><option value="2">2xx success</option><option value="4">4xx client error</option><option value="5">5xx server error</option><option value="error">All errors</option></select></label></div>
      <p className="section-copy log-privacy-note">Protected data: request bodies, prompt content, cookies, authorization headers, and token values are excluded from this log.</p>
      {visible.length ? <div className="transaction-list">{visible.map(item => {
        const isOpen = expandedId === item.id;
        const isError = Number(item.status_code) >= 400;
        const execution = item.retrieval;
        return <article className={`transaction-row ${isError ? "has-error" : ""}`} key={item.id}><button type="button" className="transaction-summary" onClick={() => setExpandedId(isOpen ? null : item.id)} aria-expanded={isOpen}><span className="transaction-route"><b className={`http-method ${item.method?.toLowerCase()}`}>{item.method}</b><code>{item.path}</code><small>{new Date(item.created_at).toLocaleString()} · {item.authentication}{execution ? " · retrieval trace" : ""}</small></span><span className={`transaction-status ${isError ? "error" : ""}`}>{item.status_code}</span><span className="transaction-duration">{item.duration_ms} ms</span></button>{isOpen && <div className="transaction-detail"><div><span>Request ID</span><code>{item.request_id}</code></div><div><span>Transaction</span><code>{item.method} {item.path} → {item.status_code} in {item.duration_ms} ms</code></div>{execution && <RetrievalExecutionTrace execution={execution}/>}<p>Use the request ID to correlate this entry with structured service logs and MCP activity. No request content is retained.</p></div>}</article>;
      })}</div> : <EmptyState isCompact title="No matching transactions" description="Try removing a filter or refresh the logs."/>}
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

function Overview({selectedKb, kbs, documents, completedDocuments, processingDocuments, onViewDocuments, onSearch, onCreate}) {
  const onboardingStep = !kbs.length ? 1 : !documents.length ? 2 : processingDocuments ? 3 : 4;
  return <><PageHeading eyebrow="WORKSPACE OVERVIEW" title={selectedKb ? `Welcome back to ${selectedKb.name}` : "Build a knowledge workspace"} description="Upload trusted material, let the platform organize it, then ask questions with clear sources." actions={<Button label={selectedKb ? "Ask a question" : "Create Knowledge Base"} variant="primary" onClick={selectedKb ? onSearch : onCreate}/>}/>
    <section className="metric-grid"><Metric value={kbs.length} label="Knowledge Bases" detail="Organized domains of knowledge"/><Metric value={documents.length} label="Documents" detail={`${completedDocuments} ready to search`}/><Metric value={processingDocuments} label="Processing" detail={processingDocuments ? "We will notify you when ready" : "Nothing is waiting"}/><Metric value={selectedKb ? "Ready" : "Set up"} label="Search status" detail={selectedKb ? "Cited answers are available" : "Create a KB to begin"}/></section>
    <Card padding={4} variant="blue"><div className="onboarding"><div><p className="eyebrow">GET STARTED</p><h2>Your next best step</h2><p>{onboardingStep === 1 ? "Create a Knowledge Base for a team, project, or subject." : onboardingStep === 2 ? "Upload a document into your selected Knowledge Base." : onboardingStep === 3 ? "Your documents are being prepared. You can safely leave this page." : "Your knowledge is ready. Ask a question and review its citations."}</p></div><ol className="stepper"><li className={onboardingStep >= 1 ? "done" : ""}>Create KB</li><li className={onboardingStep >= 2 ? "done" : ""}>Upload</li><li className={onboardingStep >= 3 ? "done" : ""}>Process</li><li className={onboardingStep >= 4 ? "done" : ""}>Ask</li></ol><Button label={onboardingStep === 1 ? "Create Knowledge Base" : onboardingStep === 2 ? "Upload document" : onboardingStep === 3 ? "View processing" : "Search knowledge"} variant="primary" onClick={onboardingStep === 1 ? onCreate : onboardingStep < 4 ? onViewDocuments : onSearch}/></div></Card>
    <section className="two-column"><Card padding={4}><h2>Recent documents</h2>{documents.length ? <div className="compact-list">{documents.slice(0, 4).map(doc => <div key={doc.id}><span>{doc.title || doc.original_filename}</span><StatusBadge status={doc.status}/></div>)}</div> : <EmptyState title="No documents yet" description="Add a document to turn this workspace into a searchable source of truth." actions={<Button label="Upload document" variant="primary" onClick={onViewDocuments}/>}/>}</Card><Card padding={4}><h2>What you can do</h2><ul className="guidance-list"><li>Search across document meaning, keywords, and relationships.</li><li>Verify every answer from its source excerpt.</li><li>Explore the systems and dependencies found in your documents.</li></ul></Card></section>
  </>;
}

function KnowledgeBases({kbs, selectedKbId, setSelectedKbId, newKbName, setNewKbName, createKb, manageKnowledgeBase, updateRetrievalConfig, onContinue}) {
  const [searchTerm, setSearchTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const normalizedSearch = searchTerm.trim().toLocaleLowerCase();
  const visibleKnowledgeBases = kbs.filter(kb => {
    const matchesSearch = !normalizedSearch || `${kb.name} ${kb.code}`.toLocaleLowerCase().includes(normalizedSearch);
    return matchesSearch && (statusFilter === "all" || kb.status === statusFilter);
  });
  return <><PageHeading eyebrow="KNOWLEDGE BASES" title="Organize knowledge by context" description="Keep documents, concepts, and relationships together so every answer stays relevant."/>
    <section className="two-column"><Card padding={4}><h2>Create a Knowledge Base</h2><p className="section-copy">Examples: IT Architecture, Security Policies, Product Documentation.</p><form className="form-stack" onSubmit={createKb}><TextInput label="Knowledge Base name" value={newKbName} onChange={setNewKbName} placeholder="e.g. IT Architecture" isRequired/><Button label="Create Knowledge Base" type="submit" variant="primary"/></form></Card><Card padding={4}><h2>How this stays organized</h2><ul className="guidance-list"><li>Each document belongs to one Knowledge Base.</li><li>Queries search the selected Knowledge Base only.</li><li>Entities and graph relationships remain scoped to that context.</li></ul></Card></section>
    {kbs.length > 0 && <section className="kb-filter-bar" aria-label="Filter Knowledge Bases"><TextInput label="Search Knowledge Bases" value={searchTerm} onChange={setSearchTerm} placeholder="Search by name or code"/><label className="native-field">Status<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">All statuses</option><option value="active">Active</option><option value="draft">Draft</option><option value="disabled">Disabled</option></select></label><p className="section-copy">{visibleKnowledgeBases.length} of {kbs.length} Knowledge Bases</p></section>}
    <section className="kb-grid">{kbs.length ? visibleKnowledgeBases.map(kb => <article className={`kb-card ${kb.id === selectedKbId ? "selected" : ""}`} key={kb.id}><button className="kb-card-select" onClick={() => setSelectedKbId(kb.id)}><span className="kb-card-title">{kb.name}</span><span className="kb-card-code">{kb.code}</span><StatusBadge status={kb.status}/></button>{kb.id === selectedKbId && <button className="kb-selected" onClick={onContinue}>Selected · continue to Documents →</button>}<div className="kb-card-actions">{kb.status === "active" ? <Button label="Disable" size="sm" variant="secondary" onClick={() => manageKnowledgeBase(kb, "disable")}/> : <Button label="Activate" size="sm" variant="secondary" onClick={() => manageKnowledgeBase(kb, "activate")}/>}<Button label="Delete" size="sm" variant="destructive" onClick={() => manageKnowledgeBase(kb, "delete")}/></div>{kb.id === selectedKbId && <RetrievalPolicyEditor knowledgeBase={kb} onSave={config => updateRetrievalConfig(kb, config)}/>}</article>) : <EmptyState title="Create your first Knowledge Base" description="Start with one focused domain. You can add more as your organization grows."/>}{kbs.length > 0 && !visibleKnowledgeBases.length && <EmptyState isCompact title="No Knowledge Bases match" description="Try another name, code, or status filter."/>}</section>
    {selectedKbId && <div className="page-footer-action"><Button label="Continue to documents" variant="primary" onClick={onContinue}/></div>}
  </>;
}

function RetrievalPolicyEditor({knowledgeBase, onSave}) {
  const current = knowledgeBase.retrieval_config || {};
  const [draft, setDraft] = useState(current);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(current), [knowledgeBase.id, knowledgeBase.retrieval_config]);
  const toggle = key => setDraft(value => ({...value, [key]: !value[key]}));
  const save = async event => { event.preventDefault(); setSaving(true); try { await onSave(draft); } finally { setSaving(false); } };
  return <details className="retrieval-policy"><summary>Retrieval policy</summary><form className="retrieval-policy-form" onSubmit={save}><p className="section-copy">Controls which stores the planner may use for this Knowledge Base.</p><label className="native-field">Mode<select value={draft.retrieval_mode || "auto"} onChange={event => setDraft({...draft, retrieval_mode: event.target.value})}><option value="auto">Auto</option><option value="balanced">Balanced</option><option value="precision">Precision</option><option value="recall">Recall</option></select></label><div className="policy-checks">{[["enable_vector","Semantic vector"],["enable_fulltext","Full-text"],["enable_graph","Graph"],["enable_lightrag","LightRAG"],["enable_reranker","Reranker"],["planner_llm_fallback","LLM fallback for ambiguous queries"]].map(([key,label]) => <label key={key}><input type="checkbox" checked={draft[key] !== false} onChange={() => toggle(key)}/>{label}</label>)}</div><div className="policy-numbers"><label>Default top-k<input type="number" min="1" max="30" value={draft.default_top_k || 12} onChange={event => setDraft({...draft, default_top_k: Number(event.target.value)})}/></label><label>Graph depth<input type="number" min="1" max="3" value={draft.maximum_graph_depth || 3} onChange={event => setDraft({...draft, maximum_graph_depth: Number(event.target.value)})}/></label></div><Button label="Save retrieval policy" type="submit" size="sm" variant="secondary" isLoading={saving}/></form></details>;
}

function Documents({selectedKb, documents, showDeletedDocuments, setShowDeletedDocuments, uploadFile, setUploadFile, uploadTitle, setUploadTitle, uploadDocumentType, setUploadDocumentType, uploadDocument, isUploading, openDocument, extractLegalMetadata, saveLegalMetadata, deleteLegalMetadata, reprocessDocument, deleteDocument, restoreDocument, reindexEmbeddings, refreshDocuments, documentPreview, documentJobs, onCreateKb, onSearch, onExplore}) {
  if (!selectedKb) return <EmptyState title="Create a Knowledge Base first" description="Documents need a context so search results remain relevant and secure." actions={<Button label="Create Knowledge Base" variant="primary" onClick={onCreateKb}/>}/>;
  return <><PageHeading eyebrow="DOCUMENTS" title={`Build ${selectedKb.name}`} description="Drag in a file. We validate it, extract its text, prepare citations, and make it searchable." actions={<><Button label={showDeletedDocuments ? "Hide deleted" : "Show deleted"} variant="ghost" onClick={() => setShowDeletedDocuments(value => !value)}/><Button label="Reindex embeddings" variant="secondary" onClick={reindexEmbeddings}/><Button label="Refresh status" variant="ghost" onClick={refreshDocuments}/></>}/>
    <Card padding={4} variant="muted"><form className="upload-layout" onSubmit={uploadDocument}><FileInput label="Add a document" value={uploadFile} onChange={files => setUploadFile(Array.isArray(files) ? files[0] : files)} accept={ACCEPTED_FILES} maxSize={MAX_FILE_SIZE} mode="dropzone" description="PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML, CSV, or JSON · up to 50 MB" isLoading={isUploading}/><div className="upload-meta"><label className="native-field">Document type<select value={uploadDocumentType} onChange={event => setUploadDocumentType(event.target.value)} disabled={isUploading}>{DOCUMENT_TYPE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label><p className="section-copy document-type-help">{DOCUMENT_TYPE_OPTIONS.find(option => option.value === uploadDocumentType)?.description}</p><TextInput label="Document title" value={uploadTitle} onChange={setUploadTitle} placeholder="Optional display title" isOptional/><Button label="Upload and process" type="submit" variant="primary" isDisabled={!uploadFile} isLoading={isUploading}/></div></form><p className="section-copy upload-format-note">Office files are converted to structured Markdown for search, citations, and legal review. Original files remain unchanged.</p></Card>
    <section className="content-section"><div className="section-title"><div><h2>{showDeletedDocuments ? "All documents" : "Library"}</h2><p>{documents.length ? `${documents.length} document${documents.length === 1 ? "" : "s"} in this Knowledge Base` : "Your uploaded documents will appear here."}</p></div>{documents.some(document => ["queued", "extracting", "indexing"].includes(document.status)) && <span className="live-status" role="status">Updating automatically</span>}</div>{documents.length ? <div className="document-table">{documents.map(document => <article key={document.id} className="document-item"><div className="document-main"><button className="document-title" onClick={() => openDocument(document)}>{document.title || document.original_filename}</button><p>{document.original_filename} · {Math.ceil(document.file_size / 1024)} KB · {documentTypeLabel(document.document_type)}</p>{["queued", "extracting", "indexing"].includes(document.status) && <><ProgressBar label={`${document.title || document.original_filename} processing`} value={100} variant="warning" isIndeterminate/><p className="document-status-help">{STATUS_HELP[document.status]}</p></>}{STATUS_HELP[document.status] && ["failed", "ocr_required"].includes(document.status) && <p className="document-status-help document-status-warning">{STATUS_HELP[document.status]}{document.error_code ? ` (${document.error_code})` : ""}</p>}</div><StatusBadge status={document.status}/><div className="document-actions"><Button label="Open details" variant="ghost" size="sm" onClick={() => openDocument(document)}/>{document.deleted_at ? <Button label="Restore" variant="secondary" size="sm" onClick={() => restoreDocument(document)}/> : <><Button label="Process again" variant="secondary" size="sm" isDisabled={["queued", "extracting", "indexing"].includes(document.status)} onClick={() => reprocessDocument(document)}/><Button label="Delete" variant="destructive" size="sm" onClick={() => deleteDocument(document)}/></>}</div></article>)}</div> : <EmptyState title="Your library is ready for its first document" description="Use the drop zone above. We will show processing progress and tell you if anything needs attention."/>}</section>
    {documents.some(document => document.status === "completed") && <section className="next-step-card"><div><p className="eyebrow">NEXT STEP</p><h2>Your knowledge is ready to use</h2><p>Ask a question for cited answers, or explore the entities and relationships found in your documents.</p></div><div className="next-step-actions"><Button label="Search knowledge" variant="primary" onClick={onSearch}/><Button label="Explore graph" variant="secondary" onClick={onExplore}/></div></section>}
    {documentPreview && <DocumentPreview preview={documentPreview} jobs={documentJobs} onExtractLegal={extractLegalMetadata} onSaveLegal={saveLegalMetadata} onDeleteLegal={deleteLegalMetadata}/>}</>
}

function SearchView({selectedKb, documents, completedDocuments, query, setQuery, runQuery, isQuerying, queryResult, submitFeedback, onDocuments, onOpenSource}) {
  if (!selectedKb) return <EmptyState title="Select a Knowledge Base to search" description="Create a Knowledge Base and upload documents first." actions={<Button label="Go to documents" variant="primary" onClick={onDocuments}/>}/>;
  if (!completedDocuments) return <EmptyState title="Finish preparing a document first" description={documents.length ? "Your document is still being processed. Return to Documents to follow its progress." : "Upload a document to create searchable knowledge for this Knowledge Base."} actions={<Button label={documents.length ? "View processing" : "Upload document"} variant="primary" onClick={onDocuments}/>}/>;
  const examples = ["What systems depend on the database?", "Summarize the main architecture decisions.", "What is the impact if this service stops working?"];
  return <><PageHeading eyebrow="SEARCH" title="Ask your knowledge" description={`Answers search ${selectedKb.name} and always show the evidence they are based on.`}/><Card padding={4} variant="blue"><form className="search-form" onSubmit={runQuery}><TextArea label="Your question" value={query} onChange={setQuery} rows={4} placeholder="Ask a clear question about this Knowledge Base" isRequired/><div className="example-row"><span>Try an example:</span>{examples.map(example => <button key={example} type="button" className="example-chip" onClick={() => setQuery(example)}>{example}</button>)}</div><Button label={isQuerying ? "Searching knowledge…" : "Search knowledge"} type="submit" variant="primary" size="lg" isDisabled={!query.trim() || isQuerying} isLoading={isQuerying}/>{isQuerying && <p className="query-progress" role="status" aria-live="polite">กำลังค้นหาความหมาย คีย์เวิร์ด ความสัมพันธ์ และแหล่งอ้างอิง…</p>}</form></Card>{queryResult && <QueryResult data={queryResult} submitFeedback={submitFeedback} onOpenSource={onOpenSource}/>}</>;
}

function ExploreView({selectedKb, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship}) {
  if (!selectedKb) return <EmptyState title="Choose a Knowledge Base to explore" description="Relationships and impact analysis are scoped to one Knowledge Base."/>;
  const description = isLegalGraph ? "Verified legal structure is shown by default. Review suggested cross-document relationships with their evidence before approving them." : "Click a blank area to add an entity. Drag from any edge of a node to another node to connect them.";
  return <><PageHeading eyebrow="EXPLORE" title={isLegalGraph ? "Explore your legal knowledge graph" : "Explore your knowledge graph"} description={description}/>
    <GraphWorkspace knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
  </>;
}

const ENTITY_TYPES = ["Application", "Service", "Server", "Database", "BusinessProcess", "Organization", "Concept"];
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
    <span className="graph-node-type">{data.entityType}</span>
  </div>;
}

const graphNodeTypes = {knowledge: KnowledgeNode};

function GraphWorkspace(props) {
  return <ReactFlowProvider><GraphCanvas {...props}/></ReactFlowProvider>;
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

  useEffect(() => { api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`).then(data => setLayout(Object.fromEntries(data.items.map(item => [item.entity_id, {x: item.x, y: item.y}])))).catch(() => setLayout({})); }, [knowledgeBaseId]);

  useEffect(() => {
    const degree = relationships.reduce((counts, relationship) => ({...counts, [relationship.source_entity_id]: (counts[relationship.source_entity_id] || 0) + 1, [relationship.target_entity_id]: (counts[relationship.target_entity_id] || 0) + 1}), {});
    const ordered = [...entities].sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0) || a.name.localeCompare(b.name));
    const rankById = Object.fromEntries(ordered.map((entity, index) => [entity.id, index]));
    setNodes(current => entities.map((entity, index) => {
      const existing = current.find(node => node.id === entity.id);
      const rank = rankById[entity.id] ?? index;
      const ringCount = Math.max(1, entities.length - 1);
      const angle = ringCount > 1 ? ((rank - 1) / ringCount) * Math.PI * 2 - Math.PI / 2 : 0;
      const radius = Math.max(230, Math.min(360, ringCount * 48));
      const automaticPosition = rank === 0 ? {x: 520, y: 350} : {x: 520 + Math.cos(angle) * radius, y: 350 + Math.sin(angle) * radius};
      return {id: entity.id, type: "knowledge", position: existing?.position || layout[entity.id] || automaticPosition, data: {label: entity.name, entityType: entity.entity_type}};
    }));
  }, [entities, relationships, layout, setNodes]);

  useEffect(() => {
    const nodesById = Object.fromEntries(nodes.map(node => [node.id, node]));
    setEdges(relationships.map(relationship => {
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
    event.preventDefault(); if (!selectedEntity) return; await analyzeImpact({subject: selectedEntity.name, scenario});
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
  return <section className="graph-workspace"><div className="graph-toolbar"><div><Badge label={`${entities.length} entities`} variant="info"/><Badge label={`${relationships.length} relationships`} variant="neutral"/></div>{isLegalGraph ? <><label className="relationship-picker">Graph view<select value={legalGraphView} onChange={event => setLegalGraphView(event.target.value)}><option value="verified">Verified legal structure</option><option value="suggested">Suggested relationships</option><option value="manual">Manual graph</option><option value="all">All graph evidence</option></select></label><Button label={legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status) ? "Rebuilding legal graph…" : "Rebuild legal graph"} variant="secondary" size="sm" onClick={queueLegalGraphRebuild} isDisabled={Boolean(legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status))}/></> : <><label className="relationship-picker">New connection type<select value={relationshipType} onChange={event => setRelationshipType(event.target.value)}>{RELATIONSHIP_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><Button label="Import from documents" variant="secondary" size="sm" onClick={syncGraph}/></>}<Button label="Fit graph" variant="ghost" size="sm" onClick={() => fitView({padding: 0.24, duration: 280})}/></div>
    <div className="graph-layout"><div className="graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={graphNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={onPaneClick} onNodeDragStop={onNodeDragStop} onEdgeClick={selectEdge} onConnect={onConnect} fitView fitViewOptions={{padding: 0.3}} minZoom={0.25} maxZoom={2} nodesConnectable connectionMode="loose" connectionRadius={24} defaultEdgeOptions={{type: "smoothstep"}}><Background gap={20} size={1} color="#b9cbd3"/><MiniMap pannable zoomable nodeColor="#2c7282"/><Controls showInteractive={false}/></ReactFlow></div>
      <aside className={`graph-inspector ${isInspectorOpen ? "open" : "closed"}`}>{isInspectorOpen && <button type="button" className="graph-inspector-close" onClick={closeInspector} aria-label="Close inspector" style={{position: "absolute", top: 12, right: 14, border: 0, background: "transparent", color: "#52717a", fontSize: "1.5rem", lineHeight: 1, cursor: "pointer"}}>×</button>}{draftPosition ? <form className="form-stack" onSubmit={createNode}><p className="eyebrow">NEW ENTITY</p><h2>Add to graph</h2><p className="section-copy">This entity will be placed where you clicked.</p><TextInput label="Entity name" value={entityName} onChange={setEntityName} placeholder="e.g. Payment API" isRequired hasAutoFocus/><label className="native-field">Type<select value={entityType} onChange={event => setEntityType(event.target.value)}>{ENTITY_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><Button label="Add entity" type="submit" variant="primary" isDisabled={!entityName.trim()}/></form> : selectedEntity ? <form className="form-stack" onSubmit={updateSelectedEntity}><p className="eyebrow">SELECTED ENTITY</p><h2>Edit entity</h2><TextInput label="Entity name" value={editName} onChange={setEditName} isRequired/><label className="native-field">Type<select value={editEntityType} onChange={event => setEditEntityType(event.target.value)}>{ENTITY_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><p className="section-copy graph-help">{isLegalGraph && legalGraphView !== "manual" ? "Legal nodes are rebuilt from their sourced metadata." : `Drag from any edge of this node to another node to create a ${relationshipType.replace(/_/g, " ")} relationship.`}</p>{(!isLegalGraph || legalGraphView === "manual") && <><Button label="Save entity" type="submit" variant="primary" isDisabled={!editName.trim()}/><div className="form-stack graph-impact-form"><TextInput label="Impact scenario" value={scenario} onChange={setScenario} placeholder="e.g. stops working" isRequired/><Button label="Analyze impact" type="button" variant="secondary" onClick={() => analyzeImpact({subject: selectedEntity.name, scenario})}/></div><Button label="Delete entity" type="button" variant="destructive" onClick={deleteSelectedEntity}/></>}</form> : selectedRelationship ? <form className="form-stack" onSubmit={updateSelectedRelationship}><p className="eyebrow">{selectedRelationship.review_status === "suggested" ? "SUGGESTED LEGAL RELATIONSHIP" : "SELECTED RELATIONSHIP"}</p><h2>{selectedRelationship.relationship_type.replace(/_/g, " ")}</h2><p className="section-copy">{selectedRelationship.origin.replace(/_/g, " ")} · {selectedRelationship.review_status}</p>{selectedRelationship.sources?.length ? <div className="legal-evidence"><b>Evidence</b>{selectedRelationship.sources.map(source => <details key={`${source.document_id}-${source.excerpt}`}><summary>{source.title}</summary><p>{source.excerpt}</p></details>)}</div> : <p className="section-copy">No supporting excerpt was stored for this relationship.</p>}{selectedRelationship.origin === "ai_suggestion" && selectedRelationship.review_status === "suggested" ? <div className="preview-actions"><Button label="Approve" type="button" variant="primary" onClick={() => reviewLegalRelationship(selectedRelationship.id, "verified")}/><Button label="Reject" type="button" variant="destructive" onClick={() => reviewLegalRelationship(selectedRelationship.id, "rejected")}/></div> : (!isLegalGraph || legalGraphView === "manual") && <><label className="native-field">Relationship type<select value={editRelationshipType} onChange={event => setEditRelationshipType(event.target.value)}>{RELATIONSHIP_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><Button label="Save connection" type="submit" variant="primary"/><Button label="Delete connection" type="button" variant="destructive" onClick={deleteSelectedRelationship}/></>}</form> : null}{graphNotice && <p className="graph-notice" role="status">{graphNotice}</p>}</aside></div>
    {impact && <Impact data={impact}/>} 
  </section>;
}

function AccessView({selectedKb, knowledgeBases, tokens, auditLogs, mcpActivity, loadAccess, createMcpToken, changeTokenState}) {
  const allTools = ["search_knowledge", "find_entities", "analyze_relationships", "analyze_impact", "get_sources"];
  const activeKnowledgeBases = knowledgeBases.filter(kb => kb.status === "active");
  const [name, setName] = useState(""); const [secret, setSecret] = useState(""); const [isLoading, setIsLoading] = useState(false); const [formError, setFormError] = useState(""); const [copied, setCopied] = useState("");
  const [selectedKbs, setSelectedKbs] = useState(selectedKb ? [selectedKb.id] : []); const [tools, setTools] = useState(allTools); const [expiresAt, setExpiresAt] = useState(""); const [rpm, setRpm] = useState(60); const [concurrency, setConcurrency] = useState(5); const [timeout, setTimeoutValue] = useState(60); const [operations, setOperations] = useState(null);
  const mcpUrl = `${window.location.origin}/mcp`; const tokenForGuide = secret || "YOUR_SOFTNIX_MCP_TOKEN";
  const cliCommand = `claude mcp add --transport http softnix-knowledge \"${mcpUrl}\" --header \"Authorization: Bearer ${tokenForGuide}\"`;
  const jsonConfig = JSON.stringify({mcpServers: {"softnix-knowledge": {type: "http", url: mcpUrl, headers: {Authorization: "Bearer ${SOFTNIX_MCP_TOKEN}"}}}}, null, 2);
  const toggle = (value, current, setCurrent) => setCurrent(current.includes(value) ? current.filter(item => item !== value) : [...current, value]);
  const copy = async (value, label) => { await navigator.clipboard.writeText(value); setCopied(label); setTimeout(() => setCopied(""), 1800); };
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
  return <><PageHeading eyebrow="ACCESS & MCP" title="Connect knowledge safely" description="Create a scoped token, copy a ready-to-run configuration, then verify the connection." actions={<Button label="Refresh status" variant="ghost" onClick={() => { loadAccess(); loadOperations(); }}/>}/><section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operations?.ready?.status || "Checking system"}</b><span>{operations ? `${Object.keys(operations.ready.dependencies || {}).length} dependencies online` : "Loading dependencies"}</span></div></div><div className="mcp-endpoint"><span>Server endpoint</span><code>{mcpUrl}</code><button type="button" onClick={() => copy(mcpUrl, "endpoint")}>Copy</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">STEP 1</p><h2>Create a scoped token</h2></div><Badge label="Secret shown once" variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label="Token name" value={name} onChange={setName} placeholder="e.g. claude-code-architecture" isRequired/><div className="scope-section"><div className="scope-heading"><b>Knowledge Base access</b>{activeKnowledgeBases.length > 0 && <button type="button" onClick={() => setSelectedKbs(activeKnowledgeBases.map(kb => kb.id))}>Select all</button>}</div><p className="section-copy">Only active Knowledge Bases can be granted to an MCP token.</p><div className="scope-options">{activeKnowledgeBases.length ? activeKnowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${selectedKbs.includes(kb.id) ? "selected" : ""}`}><input type="checkbox" checked={selectedKbs.includes(kb.id)} onChange={() => toggle(kb.id, selectedKbs, setSelectedKbs)}/><span>{kb.name}</span></label>) : <p className="section-copy">No active Knowledge Bases. Activate one from Knowledge Bases before creating a token.</p>}</div></div><div className="scope-section"><div className="scope-heading"><b>Allowed tools</b><button type="button" onClick={() => setTools(allTools)}>Enable all</button></div><div className="tool-options">{allTools.map(tool => <label key={tool} className={`tool-option ${tools.includes(tool) ? "selected" : ""}`}><input type="checkbox" checked={tools.includes(tool)} onChange={() => toggle(tool, tools, setTools)}/><span>{tool.replace(/_/g, " ")}</span></label>)}</div></div><details className="advanced-options"><summary>Advanced limits</summary><div className="limit-grid"><label>Expiry (optional)<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>Requests/min<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>Concurrent requests<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>Timeout (seconds)<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error">{formError}</p>}<Button label="Create MCP token" type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !tools.length || !selectedKbs.length}/></form></Card><Card padding={4}><p className="eyebrow">STEP 2</p><h2>Connect with Claude Code</h2><p className="section-copy">Run this command on the machine where Claude Code is installed. Use a HTTPS URL for access outside this computer.</p><div className="code-panel"><div className="code-panel-top"><b>Terminal</b><button type="button" onClick={() => copy(cliCommand, "claude command")}>{copied === "claude command" ? "Copied" : "Copy command"}</button></div><pre>{cliCommand}</pre></div><ol className="mcp-steps"><li>Create the token in Step 1 and copy it immediately.</li><li>Paste the command into Terminal.</li><li>Restart Claude Code, then run <code>/mcp</code> to confirm <code>softnix-knowledge</code> is connected.</li></ol><details className="json-config"><summary>Prefer a project <code>.mcp.json</code> file?</summary><p>Store the token in <code>SOFTNIX_MCP_TOKEN</code>, not in source control.</p><div className="code-panel"><div className="code-panel-top"><b>.mcp.json</b><button type="button" onClick={() => copy(jsonConfig, "json config")}>{copied === "json config" ? "Copied" : "Copy JSON"}</button></div><pre>{jsonConfig}</pre></div></details>{secret && <div className="token-reveal"><b>New token — copy it now</b><code>{secret}</code><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? "Copied" : "Copy token"}</button></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">ACTIVE ACCESS</p><h2>Tokens</h2></div><span className="section-copy">Revoke a token immediately if a machine or credential is no longer trusted.</span></div>{tokens.length ? <div className="token-list">{tokens.map(token => <article className="token-row" key={token.id}><div><b>{token.name}</b><p>{token.token_prefix}… · {token.allowed_tools.length} tools · {token.allowed_knowledge_base_ids.length} knowledge bases</p><small>{token.requests_per_minute}/min · {token.max_concurrent_requests} concurrent · {token.query_timeout_seconds}s timeout{token.expires_at ? ` · expires ${new Date(token.expires_at).toLocaleString()}` : ""}</small></div><StatusBadge status={token.status}/><div className="document-actions">{token.status === "active" && <Button label="Disable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "disable")}/>} {token.status === "inactive" && <Button label="Enable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "enable")}/>} {token.status !== "revoked" && <Button label="Revoke" size="sm" variant="destructive" onClick={() => changeTokenState(token.id, "revoke")}/>}</div></article>)}</div> : <EmptyState title="No MCP tokens yet" description="Create a token above to connect Claude Code or another MCP client."/>}</section><section className="content-section"><h2>Recent audit activity</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || "system"} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title="No activity yet" description="Administrative actions will appear here."/>}</section></>;
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
const QueryResult = ({data, submitFeedback, onOpenSource}) => <section className="query-result"><Card padding={4}><p className="eyebrow">ANSWER</p><div className="answer-copy">{data.answer}</div><div className="feedback-actions"><span>Was this result useful?</span><Button label="Yes" size="sm" variant="secondary" onClick={() => submitFeedback(data.result_id, 1)}/><Button label="No" size="sm" variant="ghost" onClick={() => submitFeedback(data.result_id, -1)}/></div>{data.metadata?.retrieval_plan && <details className="retrieval-trace"><summary>How this answer was retrieved</summary><p>{data.metadata.retrieval_plan.intent} · {data.metadata.retrieval_plan.planner_source} · {(data.metadata.retrieval_plan.channels || []).join(", ") || "no channels selected"}</p><ul>{(data.metadata.retrieval_trace || []).map((step, index) => <li key={`${step.channel}-${index}`}><b>{step.system}</b><span>{step.status} · {step.result_count ?? 0} result(s) · {step.duration_ms ?? 0} ms</span></li>)}</ul></details>}</Card><div className="sources-heading"><h2>Sources</h2><p>Every claim should be checked against its supporting excerpt.</p></div><div className="source-grid">{data.sources.map(source => <Card key={source.citation_id} padding={3}><Badge label={source.citation_id} variant="info"/><h3>{source.title}</h3><p>{source.excerpt}</p><Button label="Open source" size="sm" variant="ghost" onClick={() => onOpenSource({id: source.document_id, title: source.title})}/></Card>)}</div></section>;
function DocumentPreview({preview, jobs, onExtractLegal, onSaveLegal, onDeleteLegal}) {
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
  return <section className="preview-section"><Card padding={4}><div className="preview-heading"><div><p className="eyebrow">DOCUMENT PREVIEW</p><h2>{preview.title}</h2></div><div className="preview-actions"><StatusBadge status={preview.status}/>{preview.status === "completed" && <Button label="Extract legal metadata" size="sm" variant="secondary" onClick={() => onExtractLegal({id: preview.document_id, title: preview.title})}/>}</div></div>{preview.error_code && <p className="inline-error">{preview.error_code}</p>}<pre className="excerpt">{preview.text || "Text will appear here when processing is complete."}</pre><div className="legal-metadata-panel"><div className="preview-heading"><div><h3>Legal metadata</h3><p className="section-copy">Legal Graph Schema v2 keeps the instrument, provisions, and cross-document references with evidence. Suggested links still require review.</p></div>{!editingLegal && <div className="preview-actions"><Button label={hasLegalMetadata ? "Edit metadata" : "Add metadata"} size="sm" variant="secondary" onClick={startEditing}/>{hasLegalMetadata && <Button label="Delete metadata" size="sm" variant="destructive" onClick={() => onDeleteLegal({id: preview.document_id, title: preview.title})}/>}</div>}</div>{editingLegal ? <form className="legal-editor" onSubmit={save}><textarea aria-label="Legal metadata JSON" value={legalDraft} onChange={event => setLegalDraft(event.target.value)} rows={18} spellCheck="false"/><p className="section-copy">Use <code>instrument</code>, <code>provisions</code>, and <code>references</code>; every fact needs an <code>evidence_quote</code>. Saving queues a safe graph rebuild.</p>{legalError && <p className="inline-error" role="alert">{legalError}</p>}<div className="preview-actions"><Button label="Save metadata" type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={() => setEditingLegal(false)}/></div></form> : hasLegalMetadata ? <pre className="excerpt legal-metadata">{JSON.stringify(preview.legal_metadata, null, 2)}</pre> : <p className="section-copy">ยังไม่มี legal metadata — กด Add metadata เพื่อเพิ่มเอง หรือ Extract legal metadata เพื่อสกัดจากเอกสาร</p>}</div><h3>Processing activity</h3>{jobs.length ? <div className="job-list">{jobs.map(job => <div key={job.id}><span>{job.type || "PROCESS_DOCUMENT"} · {job.stage || "queued"}{job.attempt_count ? ` · attempt ${job.attempt_count}` : ""}{job.error_code ? ` · ${job.error_code}` : ""}{job.error_message ? `: ${job.error_message}` : ""}</span><StatusBadge status={job.status}/><span>{job.progress_percent}%</span></div>)}</div> : <p className="section-copy">No processing jobs have been recorded yet.</p>}</Card></section>;
}

createRoot(document.getElementById("root")).render(<App/>);
