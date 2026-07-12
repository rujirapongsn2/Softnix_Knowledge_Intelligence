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
import {connectionHandles} from "./graph-geometry.mjs";

const ACCEPTED_FILES = ".pdf,.docx,.pptx,.xlsx,.xls,.txt,.md,.html,.htm,.csv,.json";
const MAX_FILE_SIZE = 50 * 1024 * 1024;

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
  const [message, setMessage] = useState(null);
  const [activeView, setActiveView] = useState("overview");
  const [newKbName, setNewKbName] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [showDeletedDocuments, setShowDeletedDocuments] = useState(false);
  const [tokens, setTokens] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
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
    setSelectedKbId(current => current || rows[0]?.id || "");
  };
  const loadKbData = async (id, includeDeleted = showDeletedDocuments) => {
    if (!id) { setEntities([]); setRelationships([]); setDocuments([]); return; }
    const [nextEntities, nextRelationships, nextDocuments] = await Promise.all([
      api(`/v1/knowledge-bases/${id}/entities`), api(`/v1/knowledge-bases/${id}/relationships`), api(`/v1/knowledge-bases/${id}/documents${includeDeleted ? "?include_deleted=true" : ""}`),
    ]);
    setEntities(nextEntities); setRelationships(nextRelationships); setDocuments(nextDocuments);
    setGraph(null); setImpact(null); setDocumentPreview(null); setDocumentJobs([]);
  };
  useEffect(() => { if (user) loadKbs().catch(showError); }, [user]);
  useEffect(() => { if (user) loadKbData(selectedKbId).catch(showError); }, [selectedKbId, user, showDeletedDocuments]);
  useEffect(() => {
    if (!user || activeView !== "documents" || !selectedKbId || processingDocuments === 0) return undefined;
    const timer = window.setInterval(() => loadKbData(selectedKbId).catch(showError), 5000);
    return () => window.clearInterval(timer);
  }, [activeView, selectedKbId, user, processingDocuments, showDeletedDocuments]);

  const createKb = async event => {
    event.preventDefault(); const name = newKbName.trim(); if (!name) return;
    try {
      const code = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "knowledge-base";
      const kb = await api("/v1/knowledge-bases", {method: "POST", body: JSON.stringify({name, code})});
      setKbs(items => [...items, kb]); setSelectedKbId(kb.id); setNewKbName(""); setActiveView("documents"); notify("Knowledge Base created. Upload your first document to begin.");
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
  const runQuery = async event => {
    event.preventDefault();
    try { setQueryResult(await api("/v1/query", {method: "POST", body: JSON.stringify({query, knowledge_base_ids: selectedKbId ? [selectedKbId] : []})})); }
    catch (error) { showError(error); }
  };
  const analyzeImpact = async ({subject, scenario}) => {
    if (!selectedKbId || !subject?.trim() || !scenario?.trim()) return;
    try { setImpact(await api("/v1/query/impact", {method: "POST", body: JSON.stringify({subject: subject.trim(), scenario: scenario.trim(), knowledge_base_ids: [selectedKbId], max_depth: 3})})); }
    catch (error) { showError(error); }
  };
  const uploadDocument = async event => {
    event.preventDefault(); if (!selectedKbId || !uploadFile) return;
    const form = new FormData(); form.append("file", uploadFile); if (uploadTitle.trim()) form.append("title", uploadTitle.trim());
    setIsUploading(true);
    try {
      const created = await api(`/v1/knowledge-bases/${selectedKbId}/documents`, {method: "POST", body: form});
      setUploadFile(null); setUploadTitle(""); await loadKbData(selectedKbId); await openDocument({id: created.document_id, title: uploadTitle.trim() || uploadFile.name}); notify("Upload received. Processing details are shown below.");
    } catch (error) { showError(error); }
    finally { setIsUploading(false); }
  };
  const extractLegalMetadata = async document => {
    try { await api(`/v1/documents/${document.id}/legal-extract`, {method: "POST"}); await openDocument(document); notify("Legal metadata extraction queued. Review the result when processing completes."); }
    catch (error) { showError(error); }
  };
  const saveLegalMetadata = async (document, metadata) => {
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "PUT", body: JSON.stringify({metadata})}); await openDocument(document); notify("Legal metadata saved."); }
    catch (error) { showError(error); throw error; }
  };
  const deleteLegalMetadata = async document => {
    if (!window.confirm("Delete all legal metadata for this document?")) return;
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "DELETE"}); await openDocument(document); notify("Legal metadata deleted."); }
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
    const [nextTokens, nextAudit] = await Promise.all([api("/v1/tokens"), api("/v1/audit-logs?limit=20")]);
    setTokens(nextTokens); setAuditLogs(nextAudit);
  };
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
    <SideNavSection title="SYSTEM"><SideNavItem label="Access & MCP" isSelected={activeView === "access"} onClick={() => switchView("access")}/></SideNavSection>
  </SideNav>;
  const topNav = <TopNav label="Workspace navigation" heading={<TopNavHeading heading={selectedKb?.name || "Knowledge workspace"}/>} endContent={<div className="topnav-user"><span className="status-indicator"/> {user.username}</div>}/>;

  return <Theme theme={neutralTheme}><AppShell topNav={topNav} sideNav={sideNav} mobileNav={{breakpoint: "md"}} height="auto" variant="elevated" contentPadding={4}>
    <div className="workspace" aria-live="polite">
      {message && <Toast body={message.body} type={message.type} isAutoHide={message.type !== "error"} autoHideDuration={5000} onDismiss={() => setMessage(null)}/>} 
      {activeView === "overview" && <Overview selectedKb={selectedKb} kbs={kbs} documents={documents} completedDocuments={completedDocuments} processingDocuments={processingDocuments} onViewDocuments={() => switchView("documents")} onSearch={() => switchView("search")} onCreate={() => switchView("knowledge-bases")}/>}
      {activeView === "knowledge-bases" && <KnowledgeBases kbs={kbs} selectedKbId={selectedKbId} setSelectedKbId={setSelectedKbId} newKbName={newKbName} setNewKbName={setNewKbName} createKb={createKb} onContinue={() => switchView("documents")}/>}
      {activeView === "documents" && <Documents selectedKb={selectedKb} documents={documents} showDeletedDocuments={showDeletedDocuments} setShowDeletedDocuments={setShowDeletedDocuments} uploadFile={uploadFile} setUploadFile={setUploadFile} uploadTitle={uploadTitle} setUploadTitle={setUploadTitle} uploadDocument={uploadDocument} isUploading={isUploading} openDocument={openDocument} extractLegalMetadata={extractLegalMetadata} saveLegalMetadata={saveLegalMetadata} deleteLegalMetadata={deleteLegalMetadata} reprocessDocument={reprocessDocument} deleteDocument={deleteDocument} restoreDocument={restoreDocument} reindexEmbeddings={reindexEmbeddings} refreshDocuments={() => loadKbData(selectedKbId).catch(showError)} documentPreview={documentPreview} documentJobs={documentJobs} onCreateKb={() => switchView("knowledge-bases")} onSearch={() => switchView("search")} onExplore={() => switchView("explore")}/>} 
      {activeView === "search" && <SearchView selectedKb={selectedKb} documents={documents} completedDocuments={completedDocuments} query={query} setQuery={setQuery} runQuery={runQuery} queryResult={queryResult} submitFeedback={submitQueryFeedback} onDocuments={() => switchView("documents")} onOpenSource={document => { switchView("documents"); openDocument(document); }}/>} 
      {activeView === "explore" && <ExploreView selectedKb={selectedKb} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)}/>} 
      {activeView === "access" && <AccessView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} loadAccess={loadAccess} createMcpToken={createMcpToken} changeTokenState={changeTokenState}/>} 
    </div>
  </AppShell></Theme>;
}

const PageHeading = ({eyebrow, title, description, actions}) => <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</div>;
const STATUS_LABELS = {queued: "Waiting to start", extracting: "Reading text", indexing: "Building search index", completed: "Ready", failed: "Needs attention", ocr_required: "OCR required"};
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

function KnowledgeBases({kbs, selectedKbId, setSelectedKbId, newKbName, setNewKbName, createKb, onContinue}) {
  return <><PageHeading eyebrow="KNOWLEDGE BASES" title="Organize knowledge by context" description="Keep documents, concepts, and relationships together so every answer stays relevant."/>
    <section className="two-column"><Card padding={4}><h2>Create a Knowledge Base</h2><p className="section-copy">Examples: IT Architecture, Security Policies, Product Documentation.</p><form className="form-stack" onSubmit={createKb}><TextInput label="Knowledge Base name" value={newKbName} onChange={setNewKbName} placeholder="e.g. IT Architecture" isRequired/><Button label="Create Knowledge Base" type="submit" variant="primary"/></form></Card><Card padding={4}><h2>How this stays organized</h2><ul className="guidance-list"><li>Each document belongs to one Knowledge Base.</li><li>Queries search the selected Knowledge Base only.</li><li>Entities and graph relationships remain scoped to that context.</li></ul></Card></section>
    <section className="kb-grid">{kbs.length ? kbs.map(kb => <button className={`kb-card ${kb.id === selectedKbId ? "selected" : ""}`} key={kb.id} onClick={() => setSelectedKbId(kb.id)}><span className="kb-card-title">{kb.name}</span><span className="kb-card-code">{kb.code}</span><StatusBadge status={kb.status}/>{kb.id === selectedKbId && <span className="kb-selected">Selected · continue to Documents →</span>}</button>) : <EmptyState title="Create your first Knowledge Base" description="Start with one focused domain. You can add more as your organization grows."/>}</section>
    {selectedKbId && <div className="page-footer-action"><Button label="Continue to documents" variant="primary" onClick={onContinue}/></div>}
  </>;
}

function Documents({selectedKb, documents, showDeletedDocuments, setShowDeletedDocuments, uploadFile, setUploadFile, uploadTitle, setUploadTitle, uploadDocument, isUploading, openDocument, extractLegalMetadata, saveLegalMetadata, deleteLegalMetadata, reprocessDocument, deleteDocument, restoreDocument, reindexEmbeddings, refreshDocuments, documentPreview, documentJobs, onCreateKb, onSearch, onExplore}) {
  if (!selectedKb) return <EmptyState title="Create a Knowledge Base first" description="Documents need a context so search results remain relevant and secure." actions={<Button label="Create Knowledge Base" variant="primary" onClick={onCreateKb}/>}/>;
  return <><PageHeading eyebrow="DOCUMENTS" title={`Build ${selectedKb.name}`} description="Drag in a file. We validate it, extract its text, prepare citations, and make it searchable." actions={<><Button label={showDeletedDocuments ? "Hide deleted" : "Show deleted"} variant="ghost" onClick={() => setShowDeletedDocuments(value => !value)}/><Button label="Reindex embeddings" variant="secondary" onClick={reindexEmbeddings}/><Button label="Refresh status" variant="ghost" onClick={refreshDocuments}/></>}/>
    <Card padding={4} variant="muted"><form className="upload-layout" onSubmit={uploadDocument}><FileInput label="Add a document" value={uploadFile} onChange={files => setUploadFile(Array.isArray(files) ? files[0] : files)} accept={ACCEPTED_FILES} maxSize={MAX_FILE_SIZE} mode="dropzone" description="PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML, CSV, or JSON · up to 50 MB" isLoading={isUploading}/><div className="upload-meta"><TextInput label="Document title" value={uploadTitle} onChange={setUploadTitle} placeholder="Optional display title" isOptional/><Button label="Upload and process" type="submit" variant="primary" isDisabled={!uploadFile} isLoading={isUploading}/></div></form><p className="section-copy upload-format-note">Office files are converted to structured Markdown for search, citations, and legal review. Original files remain unchanged.</p></Card>
    <section className="content-section"><div className="section-title"><div><h2>{showDeletedDocuments ? "All documents" : "Library"}</h2><p>{documents.length ? `${documents.length} document${documents.length === 1 ? "" : "s"} in this Knowledge Base` : "Your uploaded documents will appear here."}</p></div>{documents.some(document => ["queued", "extracting", "indexing"].includes(document.status)) && <span className="live-status" role="status">Updating automatically</span>}</div>{documents.length ? <div className="document-table">{documents.map(document => <article key={document.id} className="document-item"><div className="document-main"><button className="document-title" onClick={() => openDocument(document)}>{document.title || document.original_filename}</button><p>{document.original_filename} · {Math.ceil(document.file_size / 1024)} KB</p>{["queued", "extracting", "indexing"].includes(document.status) && <><ProgressBar label={`${document.title || document.original_filename} processing`} value={100} variant="warning" isIndeterminate/><p className="document-status-help">{STATUS_HELP[document.status]}</p></>}{STATUS_HELP[document.status] && ["failed", "ocr_required"].includes(document.status) && <p className="document-status-help document-status-warning">{STATUS_HELP[document.status]}{document.error_code ? ` (${document.error_code})` : ""}</p>}</div><StatusBadge status={document.status}/><div className="document-actions"><Button label="Open details" variant="ghost" size="sm" onClick={() => openDocument(document)}/>{document.deleted_at ? <Button label="Restore" variant="secondary" size="sm" onClick={() => restoreDocument(document)}/> : <><Button label="Process again" variant="secondary" size="sm" onClick={() => reprocessDocument(document)}/><Button label="Delete" variant="destructive" size="sm" onClick={() => deleteDocument(document)}/></>}</div></article>)}</div> : <EmptyState title="Your library is ready for its first document" description="Use the drop zone above. We will show processing progress and tell you if anything needs attention."/>}</section>
    {documents.some(document => document.status === "completed") && <section className="next-step-card"><div><p className="eyebrow">NEXT STEP</p><h2>Your knowledge is ready to use</h2><p>Ask a question for cited answers, or explore the entities and relationships found in your documents.</p></div><div className="next-step-actions"><Button label="Search knowledge" variant="primary" onClick={onSearch}/><Button label="Explore graph" variant="secondary" onClick={onExplore}/></div></section>}
    {documentPreview && <DocumentPreview preview={documentPreview} jobs={documentJobs} onExtractLegal={extractLegalMetadata} onSaveLegal={saveLegalMetadata} onDeleteLegal={deleteLegalMetadata}/>}</>
}

function SearchView({selectedKb, documents, completedDocuments, query, setQuery, runQuery, queryResult, submitFeedback, onDocuments, onOpenSource}) {
  if (!selectedKb) return <EmptyState title="Select a Knowledge Base to search" description="Create a Knowledge Base and upload documents first." actions={<Button label="Go to documents" variant="primary" onClick={onDocuments}/>}/>;
  if (!completedDocuments) return <EmptyState title="Finish preparing a document first" description={documents.length ? "Your document is still being processed. Return to Documents to follow its progress." : "Upload a document to create searchable knowledge for this Knowledge Base."} actions={<Button label={documents.length ? "View processing" : "Upload document"} variant="primary" onClick={onDocuments}/>}/>;
  const examples = ["What systems depend on the database?", "Summarize the main architecture decisions.", "What is the impact if this service stops working?"];
  return <><PageHeading eyebrow="SEARCH" title="Ask your knowledge" description={`Answers search ${selectedKb.name} and always show the evidence they are based on.`}/><Card padding={4} variant="blue"><form className="search-form" onSubmit={runQuery}><TextArea label="Your question" value={query} onChange={setQuery} rows={4} placeholder="Ask a clear question about this Knowledge Base" isRequired/><div className="example-row"><span>Try an example:</span>{examples.map(example => <button key={example} type="button" className="example-chip" onClick={() => setQuery(example)}>{example}</button>)}</div><Button label="Search knowledge" type="submit" variant="primary" size="lg" isDisabled={!query.trim()}/></form></Card>{queryResult && <QueryResult data={queryResult} submitFeedback={submitFeedback} onOpenSource={onOpenSource}/>}</>;
}

function ExploreView({selectedKb, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph}) {
  if (!selectedKb) return <EmptyState title="Choose a Knowledge Base to explore" description="Relationships and impact analysis are scoped to one Knowledge Base."/>;
  return <><PageHeading eyebrow="EXPLORE" title="Explore your knowledge graph" description="Click a blank area to add an entity. Drag from any edge of a node to another node to connect them."/>
    <GraphWorkspace knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph}/>
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

function GraphCanvas({knowledgeBaseId, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph}) {
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
    const point = screenToFlowPosition({x: event.clientX, y: event.clientY});
    setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(point); setEntityName(""); setGraphNotice("Name the new entity in the panel, then add it to this position.");
  }, [screenToFlowPosition]);
  const onNodeClick = useCallback((_, node) => { setSelectedEntityId(node.id); setSelectedRelationshipId(""); setDraftPosition(null); setGraphNotice(""); }, []);
  const onConnect = useCallback(async connection => {
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const duplicate = relationships.some(item => item.source_entity_id === connection.source && item.target_entity_id === connection.target && item.relationship_type === relationshipType);
    if (duplicate) { setGraphNotice("This relationship already exists."); return; }
    const created = await addRelationship({sourceEntityId: connection.source, targetEntityId: connection.target, relationshipType});
    if (created) setGraphNotice(`Connection created: ${relationshipType.replace(/_/g, " ")}.`);
  }, [addRelationship, relationshipType, relationships]);
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

  const isInspectorOpen = Boolean(draftPosition || selectedEntity || selectedRelationship || graphNotice);
  return <section className="graph-workspace"><div className="graph-toolbar"><div><Badge label={`${entities.length} entities`} variant="info"/><Badge label={`${relationships.length} relationships`} variant="neutral"/></div><label className="relationship-picker">New connection type<select value={relationshipType} onChange={event => setRelationshipType(event.target.value)}>{RELATIONSHIP_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><Button label="Import from documents" variant="secondary" size="sm" onClick={syncGraph}/><Button label="Fit graph" variant="ghost" size="sm" onClick={() => fitView({padding: 0.24, duration: 280})}/></div>
    <div className="graph-layout"><div className="graph-canvas"><ReactFlow nodes={nodes} edges={edges} nodeTypes={graphNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={onPaneClick} onNodeDragStop={onNodeDragStop} onEdgeClick={selectEdge} onConnect={onConnect} fitView fitViewOptions={{padding: 0.3}} minZoom={0.25} maxZoom={2} nodesConnectable connectionMode="loose" connectionRadius={24} defaultEdgeOptions={{type: "smoothstep"}}><Background gap={20} size={1} color="#b9cbd3"/><MiniMap pannable zoomable nodeColor="#2c7282"/><Controls showInteractive={false}/></ReactFlow></div>
      <aside className={`graph-inspector ${isInspectorOpen ? "open" : "closed"}`}>{draftPosition ? <form className="form-stack" onSubmit={createNode}><p className="eyebrow">NEW ENTITY</p><h2>Add to graph</h2><p className="section-copy">This entity will be placed where you clicked.</p><TextInput label="Entity name" value={entityName} onChange={setEntityName} placeholder="e.g. Payment API" isRequired hasAutoFocus/><label className="native-field">Type<select value={entityType} onChange={event => setEntityType(event.target.value)}>{ENTITY_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><Button label="Add entity" type="submit" variant="primary" isDisabled={!entityName.trim()}/></form> : selectedEntity ? <form className="form-stack" onSubmit={updateSelectedEntity}><p className="eyebrow">SELECTED ENTITY</p><h2>Edit entity</h2><TextInput label="Entity name" value={editName} onChange={setEditName} isRequired/><label className="native-field">Type<select value={editEntityType} onChange={event => setEditEntityType(event.target.value)}>{ENTITY_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><p className="section-copy graph-help">Drag from any edge of this node to another node to create a {relationshipType.replace(/_/g, " ")} relationship.</p><Button label="Save entity" type="submit" variant="primary" isDisabled={!editName.trim()}/><div className="form-stack graph-impact-form"><TextInput label="Impact scenario" value={scenario} onChange={setScenario} placeholder="e.g. stops working" isRequired/><Button label="Analyze impact" type="button" variant="secondary" onClick={() => analyzeImpact({subject: selectedEntity.name, scenario})}/></div><Button label="Delete entity" type="button" variant="destructive" onClick={deleteSelectedEntity}/></form> : selectedRelationship ? <form className="form-stack" onSubmit={updateSelectedRelationship}><p className="eyebrow">SELECTED RELATIONSHIP</p><h2>Edit connection</h2><label className="native-field">Relationship type<select value={editRelationshipType} onChange={event => setEditRelationshipType(event.target.value)}>{RELATIONSHIP_TYPES.map(type => <option key={type}>{type}</option>)}</select></label><p className="section-copy graph-help">Click a node or blank canvas when you are ready to select something else.</p><Button label="Save connection" type="submit" variant="primary"/><Button label="Delete connection" type="button" variant="destructive" onClick={deleteSelectedRelationship}/></form> : null}{graphNotice && <p className="graph-notice" role="status">{graphNotice}</p>}</aside></div>
    {impact && <Impact data={impact}/>} 
  </section>;
}

function AccessView({selectedKb, knowledgeBases, tokens, auditLogs, loadAccess, createMcpToken, changeTokenState}) {
  const allTools = ["search_knowledge", "find_entities", "analyze_relationships", "analyze_impact", "get_sources"];
  const [name, setName] = useState(""); const [secret, setSecret] = useState(""); const [isLoading, setIsLoading] = useState(false); const [formError, setFormError] = useState(""); const [copied, setCopied] = useState("");
  const [selectedKbs, setSelectedKbs] = useState(selectedKb ? [selectedKb.id] : []); const [tools, setTools] = useState(allTools); const [expiresAt, setExpiresAt] = useState(""); const [rpm, setRpm] = useState(60); const [concurrency, setConcurrency] = useState(5); const [timeout, setTimeoutValue] = useState(60); const [operations, setOperations] = useState(null);
  const mcpUrl = `${window.location.origin}/mcp`; const tokenForGuide = secret || "YOUR_SOFTNIX_MCP_TOKEN";
  const cliCommand = `claude mcp add --transport http softnix-knowledge \"${mcpUrl}\" --header \"Authorization: Bearer ${tokenForGuide}\"`;
  const jsonConfig = JSON.stringify({mcpServers: {"softnix-knowledge": {type: "http", url: mcpUrl, headers: {Authorization: "Bearer ${SOFTNIX_MCP_TOKEN}"}}}}, null, 2);
  const toggle = (value, current, setCurrent) => setCurrent(current.includes(value) ? current.filter(item => item !== value) : [...current, value]);
  const copy = async (value, label) => { await navigator.clipboard.writeText(value); setCopied(label); setTimeout(() => setCopied(""), 1800); };
  const loadOperations = async () => { const [ready, projection] = await Promise.all([api("/v1/system/status"), api("/v1/system/graph-projection")]); setOperations({ready, projection}); };
  useEffect(() => { loadAccess().catch(() => undefined); loadOperations().catch(() => undefined); }, []);
  useEffect(() => { if (selectedKb) setSelectedKbs(current => current.length ? current : [selectedKb.id]); }, [selectedKb]);
  const create = async event => { event.preventDefault(); setIsLoading(true); setFormError(""); try { const result = await createMcpToken({name, allowed_knowledge_base_ids: selectedKbs, allowed_tools: tools, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null, requests_per_minute: Number(rpm), max_concurrent_requests: Number(concurrency), query_timeout_seconds: Number(timeout)}); setSecret(result.token); setName(""); } catch (error) { setFormError(error.message); } finally { setIsLoading(false); } };
  return <><PageHeading eyebrow="ACCESS & MCP" title="Connect knowledge safely" description="Create a scoped token, copy a ready-to-run configuration, then verify the connection." actions={<Button label="Refresh status" variant="ghost" onClick={() => { loadAccess(); loadOperations(); }}/>}/><section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operations?.ready?.status || "Checking system"}</b><span>{operations ? `${Object.keys(operations.ready.dependencies || {}).length} dependencies online` : "Loading dependencies"}</span></div></div><div className="mcp-endpoint"><span>Server endpoint</span><code>{mcpUrl}</code><button type="button" onClick={() => copy(mcpUrl, "endpoint")}>Copy</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">STEP 1</p><h2>Create a scoped token</h2></div><Badge label="Secret shown once" variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label="Token name" value={name} onChange={setName} placeholder="e.g. claude-code-architecture" isRequired/><div className="scope-section"><div className="scope-heading"><b>Knowledge Base access</b><button type="button" onClick={() => setSelectedKbs(knowledgeBases.map(kb => kb.id))}>Select all</button></div><div className="scope-options">{knowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${selectedKbs.includes(kb.id) ? "selected" : ""}`}><input type="checkbox" checked={selectedKbs.includes(kb.id)} onChange={() => toggle(kb.id, selectedKbs, setSelectedKbs)}/><span>{kb.name}</span></label>)}</div></div><div className="scope-section"><div className="scope-heading"><b>Allowed tools</b><button type="button" onClick={() => setTools(allTools)}>Enable all</button></div><div className="tool-options">{allTools.map(tool => <label key={tool} className={`tool-option ${tools.includes(tool) ? "selected" : ""}`}><input type="checkbox" checked={tools.includes(tool)} onChange={() => toggle(tool, tools, setTools)}/><span>{tool.replace(/_/g, " ")}</span></label>)}</div></div><details className="advanced-options"><summary>Advanced limits</summary><div className="limit-grid"><label>Expiry (optional)<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>Requests/min<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>Concurrent requests<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>Timeout (seconds)<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error">{formError}</p>}<Button label="Create MCP token" type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !tools.length || !selectedKbs.length}/></form></Card><Card padding={4}><p className="eyebrow">STEP 2</p><h2>Connect with Claude Code</h2><p className="section-copy">Run this command on the machine where Claude Code is installed. Use a HTTPS URL for access outside this computer.</p><div className="code-panel"><div className="code-panel-top"><b>Terminal</b><button type="button" onClick={() => copy(cliCommand, "claude command")}>{copied === "claude command" ? "Copied" : "Copy command"}</button></div><pre>{cliCommand}</pre></div><ol className="mcp-steps"><li>Create the token in Step 1 and copy it immediately.</li><li>Paste the command into Terminal.</li><li>Restart Claude Code, then run <code>/mcp</code> to confirm <code>softnix-knowledge</code> is connected.</li></ol><details className="json-config"><summary>Prefer a project <code>.mcp.json</code> file?</summary><p>Store the token in <code>SOFTNIX_MCP_TOKEN</code>, not in source control.</p><div className="code-panel"><div className="code-panel-top"><b>.mcp.json</b><button type="button" onClick={() => copy(jsonConfig, "json config")}>{copied === "json config" ? "Copied" : "Copy JSON"}</button></div><pre>{jsonConfig}</pre></div></details>{secret && <div className="token-reveal"><b>New token — copy it now</b><code>{secret}</code><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? "Copied" : "Copy token"}</button></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">ACTIVE ACCESS</p><h2>Tokens</h2></div><span className="section-copy">Revoke a token immediately if a machine or credential is no longer trusted.</span></div>{tokens.length ? <div className="token-list">{tokens.map(token => <article className="token-row" key={token.id}><div><b>{token.name}</b><p>{token.token_prefix}… · {token.allowed_tools.length} tools · {token.allowed_knowledge_base_ids.length} knowledge bases</p><small>{token.requests_per_minute}/min · {token.max_concurrent_requests} concurrent · {token.query_timeout_seconds}s timeout{token.expires_at ? ` · expires ${new Date(token.expires_at).toLocaleString()}` : ""}</small></div><StatusBadge status={token.status}/><div className="document-actions">{token.status === "active" && <Button label="Disable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "disable")}/>} {token.status === "inactive" && <Button label="Enable" size="sm" variant="secondary" onClick={() => changeTokenState(token.id, "enable")}/>} {token.status !== "revoked" && <Button label="Revoke" size="sm" variant="destructive" onClick={() => changeTokenState(token.id, "revoke")}/>}</div></article>)}</div> : <EmptyState title="No MCP tokens yet" description="Create a token above to connect Claude Code or another MCP client."/>}</section><section className="content-section"><h2>Recent audit activity</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || "system"} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title="No activity yet" description="Administrative actions will appear here."/>}</section></>;
}

const Impact = ({data}) => <div className="result-panel"><h3>{data.insufficient_evidence ? "Insufficient evidence" : `Impact for ${data.subject.name}`}</h3>{data.insufficient_evidence ? <p>Upload more source material or add verified relationships before making a decision.</p> : <><h4>Direct impact</h4><ul>{data.direct_impacts.map(item => <li key={item.entity_id}>{item.name} <Badge label={item.relationship} variant="warning"/> {item.citation_ids.join(" ")}</li>)}</ul><h4>Indirect impact</h4><ul>{data.indirect_impacts.map(item => <li key={item.entity_id}>{item.path.join(" → ")} {item.citation_ids.join(" ")}</li>)}</ul></>}</div>;
const Graph = ({data}) => <div className="result-panel"><div className="graph-summary"><Badge label={`${data.nodes.length} nodes`} variant="info"/><Badge label={`${data.edges.length} connections`} variant="neutral"/></div><ul className="graph-list">{data.edges.map(edge => <li key={edge.id}><b>{data.nodes.find(node => node.id === edge.source)?.name}</b><span>{edge.type.replace(/_/g, " ")}</span><b>{data.nodes.find(node => node.id === edge.target)?.name}</b></li>)}</ul></div>;
const QueryResult = ({data, submitFeedback, onOpenSource}) => <section className="query-result"><Card padding={4}><p className="eyebrow">ANSWER</p><div className="answer-copy">{data.answer}</div><div className="feedback-actions"><span>Was this result useful?</span><Button label="Yes" size="sm" variant="secondary" onClick={() => submitFeedback(data.result_id, 1)}/><Button label="No" size="sm" variant="ghost" onClick={() => submitFeedback(data.result_id, -1)}/></div></Card><div className="sources-heading"><h2>Sources</h2><p>Every claim should be checked against its supporting excerpt.</p></div><div className="source-grid">{data.sources.map(source => <Card key={source.citation_id} padding={3}><Badge label={source.citation_id} variant="info"/><h3>{source.title}</h3><p>{source.excerpt}</p><Button label="Open source" size="sm" variant="ghost" onClick={() => onOpenSource({id: source.document_id, title: source.title})}/></Card>)}</div></section>;
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
  return <section className="preview-section"><Card padding={4}><div className="preview-heading"><div><p className="eyebrow">DOCUMENT PREVIEW</p><h2>{preview.title}</h2></div><div className="preview-actions"><StatusBadge status={preview.status}/>{preview.status === "completed" && <Button label="Extract legal metadata" size="sm" variant="secondary" onClick={() => onExtractLegal({id: preview.document_id, title: preview.title})}/>}</div></div>{preview.error_code && <p className="inline-error">{preview.error_code}</p>}<pre className="excerpt">{preview.text || "Text will appear here when processing is complete."}</pre><div className="legal-metadata-panel"><div className="preview-heading"><div><h3>Legal metadata</h3><p className="section-copy">Review extracted content before relying on it. Use <code>articles</code> for มาตรา and <code>amendments</code> for ประกาศแก้ไข.</p></div>{!editingLegal && <div className="preview-actions"><Button label={hasLegalMetadata ? "Edit metadata" : "Add metadata"} size="sm" variant="secondary" onClick={startEditing}/>{hasLegalMetadata && <Button label="Delete metadata" size="sm" variant="destructive" onClick={() => onDeleteLegal({id: preview.document_id, title: preview.title})}/>}</div>}</div>{editingLegal ? <form className="legal-editor" onSubmit={save}><textarea aria-label="Legal metadata JSON" value={legalDraft} onChange={event => setLegalDraft(event.target.value)} rows={18} spellCheck="false"/><p className="section-copy">แก้ไข JSON ได้โดยตรง เช่น เพิ่มรายการใน <code>articles</code> หรือ <code>amendments</code> พร้อม <code>evidence_quote</code>.</p>{legalError && <p className="inline-error" role="alert">{legalError}</p>}<div className="preview-actions"><Button label="Save metadata" type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={() => setEditingLegal(false)}/></div></form> : hasLegalMetadata ? <pre className="excerpt legal-metadata">{JSON.stringify(preview.legal_metadata, null, 2)}</pre> : <p className="section-copy">ยังไม่มี legal metadata — กด Add metadata เพื่อเพิ่มเอง หรือ Extract legal metadata เพื่อสกัดจากเอกสาร</p>}</div><h3>Processing activity</h3>{jobs.length ? <div className="job-list">{jobs.map(job => <div key={job.id}><span>{job.type || "PROCESS_DOCUMENT"} · {job.stage || "queued"}{job.attempt_count ? ` · attempt ${job.attempt_count}` : ""}{job.error_code ? ` · ${job.error_code}` : ""}{job.error_message ? `: ${job.error_message}` : ""}</span><StatusBadge status={job.status}/><span>{job.progress_percent}%</span></div>)}</div> : <p className="section-copy">No processing jobs have been recorded yet.</p>}</Card></section>;
}

createRoot(document.getElementById("root")).render(<App/>);
