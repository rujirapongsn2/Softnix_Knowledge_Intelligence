import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
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
import "./documents.css";
import {connectionHandles} from "./graph-geometry.mjs";

const ACCEPTED_FILES = ".pdf,.docx,.pptx,.xlsx,.xls,.txt,.md,.html,.htm,.csv,.json";
const MAX_FILE_SIZE_MB = Math.max(1, Number(import.meta.env.VITE_MAX_FILE_SIZE_MB || 100));
const MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024;
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
  if (!response.ok) {
    const message = data.error?.message || (typeof data.detail === "string" ? data.detail : data.detail?.message) || "Request failed";
    throw new Error(message);
  }
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
  const [documentTotal, setDocumentTotal] = useState(0);
  const [documentOffset, setDocumentOffset] = useState(0);
  const [documentSearch, setDocumentSearch] = useState("");
  const [documentStatusFilter, setDocumentStatusFilter] = useState("all");
  const [documentTypeFilter, setDocumentTypeFilter] = useState("all");
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [processingDocumentsTotal, setProcessingDocumentsTotal] = useState(0);
  const [hasCompletedDocuments, setHasCompletedDocuments] = useState(false);
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
  const [viewTrail, setViewTrail] = useState(["knowledge-bases"]);
  const [newKbName, setNewKbName] = useState("");
  const [uploadFile, setUploadFile] = useState([]);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadDocumentType, setUploadDocumentType] = useState("general");
  const [documentTemplates, setDocumentTemplates] = useState([]);
  const [uploadTemplateId, setUploadTemplateId] = useState("system:general");
  const [uploadMetadata, setUploadMetadata] = useState({});
  const [isUploading, setIsUploading] = useState(false);
  const [showDeletedDocuments, setShowDeletedDocuments] = useState(false);
  const [tokens, setTokens] = useState([]);
  const [auditLogs, setAuditLogs] = useState([]);
  const [transactionLogs, setTransactionLogs] = useState([]);
  const [traceLogs, setTraceLogs] = useState([]);
  const [transactionCursor, setTransactionCursor] = useState(null);
  const [traceCursor, setTraceCursor] = useState(null);
  const [legalGraphView, setLegalGraphView] = useState("verified");
  const [isLegalGraph, setIsLegalGraph] = useState(false);
  const [legalRebuildStatus, setLegalRebuildStatus] = useState(null);
  const [legalInstruments, setLegalInstruments] = useState([]);
  const selectedKb = useMemo(() => kbs.find(kb => kb.id === selectedKbId), [kbs, selectedKbId]);

  const notify = (body, type = "info") => setMessage({body, type, id: Date.now()});
  const showError = error => notify(error.message || "Something went wrong. Please try again.", "error");
  useEffect(() => {
    let active = true;
    const expireSession = () => { setUser(null); setKbs([]); setSelectedKbId(""); setActiveView("knowledge-bases"); setViewTrail(["knowledge-bases"]); setDocumentPreview(null); };
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
    if (!id) { setEntities([]); setRelationships([]); setDocuments([]); setDocumentTemplates([]); setDocumentTotal(0); setProcessingDocumentsTotal(0); setHasCompletedDocuments(false); setIsLegalGraph(false); setLegalInstruments([]); return; }
    const isDocumentsView = activeView === "documents";
    const params = new URLSearchParams({limit: "50", offset: String(isDocumentsView ? documentOffset : 0)});
    if (includeDeleted && isDocumentsView) params.set("include_deleted", "true");
    if (isDocumentsView && documentSearch.trim()) params.set("search", documentSearch.trim());
    if (isDocumentsView && documentStatusFilter !== "all") params.set("status", documentStatusFilter);
    if (isDocumentsView && documentTypeFilter !== "all") {
      const selectedTemplate = documentTemplates.find(template => template.id === documentTypeFilter);
      params.set(selectedTemplate ? "template_id" : "document_type", documentTypeFilter);
    }
    setDocumentsLoading(true);
    try {
      const [documentPage, templates] = await Promise.all([
        api(`/v1/knowledge-bases/${id}/documents/page?${params.toString()}`),
        api(`/v1/knowledge-bases/${id}/document-templates?include_inactive=true`),
      ]);
      const nextDocuments = documentPage.items || [];
      const hasLegalDocuments = Boolean(documentPage.has_legal_documents);
      const nextLegalInstruments = hasLegalDocuments ? await api(`/v1/knowledge-bases/${id}/legal-registry`) : [];
      setDocuments(nextDocuments); setDocumentTotal(documentPage.total || 0);
      setDocumentTemplates(templates || []);
      setProcessingDocumentsTotal(documentPage.processing_count || 0);
      setHasCompletedDocuments(Boolean(documentPage.has_completed_documents));
      setLegalInstruments(nextLegalInstruments);
      setIsLegalGraph(hasLegalDocuments);
      if (activeView === "explore") {
        const graphData = hasLegalDocuments
          ? await api(`/v1/knowledge-bases/${id}/legal-graph?view=${legalGraphView}`)
          : await Promise.all([api(`/v1/knowledge-bases/${id}/entities`), api(`/v1/knowledge-bases/${id}/relationships`)]);
        const [nextEntities, nextRelationships] = hasLegalDocuments ? [graphData.nodes, graphData.edges] : graphData;
        setEntities(nextEntities); setRelationships(nextRelationships); setGraph(null); setImpact(null);
      }
    } finally { setDocumentsLoading(false); }
  };
  useEffect(() => { if (user) loadKbs().catch(showError); }, [user]);
  useEffect(() => { setLegalRebuildStatus(null); }, [selectedKbId]);
  useEffect(() => { if (selectedKbId) { setDocumentOffset(0); setDocumentPreview(null); setDocumentJobs([]); setDocumentTypeFilter("all"); } }, [selectedKbId]);
  useEffect(() => { if (user) loadKbData(selectedKbId).catch(showError); }, [selectedKbId, user, showDeletedDocuments, legalGraphView, activeView, documentOffset, documentSearch, documentStatusFilter, documentTypeFilter]);
  useEffect(() => {
    if (!user || activeView !== "documents" || !selectedKbId || processingDocumentsTotal === 0) return undefined;
    const timer = window.setInterval(() => loadKbData(selectedKbId).catch(showError), 5000);
    return () => window.clearInterval(timer);
  }, [activeView, selectedKbId, user, processingDocumentsTotal, showDeletedDocuments]);
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
      setKbs(items => [...items, kb]); setSelectedKbId(kb.id); setNewKbName(""); switchView("documents"); notify("Knowledge Base created. Upload your first document to begin.");
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
    const template = documentTemplates.find(row => row.id === uploadTemplateId);
    const form = new FormData(); uploadFile.forEach(file => form.append("files", file)); form.append("document_type", template?.base_document_type || uploadDocumentType); form.append("template_id", uploadTemplateId); form.append("metadata_json", JSON.stringify(uploadMetadata)); if (uploadFile.length === 1 && uploadTitle.trim()) form.append("title", uploadTitle.trim());
    setIsUploading(true);
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/documents/batch`, {method: "POST", body: form});
      const selectedCount = uploadFile.length;
      setUploadFile([]); setUploadTitle(""); setUploadDocumentType("general"); setUploadTemplateId("system:general"); setUploadMetadata({}); await loadKbData(selectedKbId);
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
  const saveDocumentMetadata = async (document, values) => {
    try { await api(`/v1/documents/${document.id}/metadata`, {method: "PATCH", body: JSON.stringify({values})}); await openDocument(document); await loadKbData(selectedKbId); notify("Document metadata saved."); }
    catch (error) { showError(error); throw error; }
  };
  const createDocumentTemplate = async payload => {
    try { await api(`/v1/knowledge-bases/${selectedKbId}/document-templates`, {method: "POST", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify("Document type created."); }
    catch (error) { showError(error); throw error; }
  };
  const updateDocumentTemplate = async (templateId, payload) => {
    try { await api(`/v1/document-templates/${templateId}`, {method: "PATCH", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify("Document type updated."); }
    catch (error) { showError(error); throw error; }
  };
  const deactivateDocumentTemplate = async template => {
    if (!window.confirm(`Disable document type “${template.name}”? Existing documents keep their metadata.`)) return;
    try { await api(`/v1/document-templates/${template.id}`, {method: "DELETE"}); if (uploadTemplateId === template.id) { setUploadTemplateId("system:general"); setUploadDocumentType("general"); setUploadMetadata({}); } if (documentTypeFilter === template.id) { setDocumentTypeFilter("all"); setDocumentOffset(0); } await loadKbData(selectedKbId); notify("Document type archived."); }
    catch (error) { showError(error); }
  };
  const activateDocumentTemplate = async template => {
    try { await api(`/v1/document-templates/${template.id}/activate`, {method: "POST"}); await loadKbData(selectedKbId); notify("Document type activated."); }
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
    const results = await Promise.allSettled([api("/v1/tokens"), api("/v1/audit-logs?limit=20")]);
    const [tokenResult, auditResult] = results;
    if (tokenResult.status === "fulfilled") setTokens(tokenResult.value);
    if (auditResult.status === "fulfilled") setAuditLogs(auditResult.value);
    const errors = results.filter(result => result.status === "rejected").map(result => result.reason?.message || "Access data request failed");
    return {errors};
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
  const switchView = view => {
    setActiveView(view); setDocumentPreview(null);
    setViewTrail(current => current.at(-1) === view ? current : pushViewTrail(current, view));
  };
  const navigateToView = view => {
    setActiveView(view); setDocumentPreview(null);
    setViewTrail(current => pushViewTrail(current, view));
  };
  const goBack = () => {
    const next = viewTrail.length > 1 ? viewTrail.slice(0, -1) : viewTrail;
    setViewTrail(next);
    setActiveView(next.at(-1) || "knowledge-bases");
    setDocumentPreview(null);
  };
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
      <WorkflowNavigation activeView={activeView} selectedKb={selectedKb} hasCompletedDocuments={hasCompletedDocuments} viewTrail={viewTrail} onNavigate={navigateToView} onBack={goBack} onNavigateNext={switchView}/>
      {activeView === "knowledge-bases" && <KnowledgeBases kbs={kbs} selectedKbId={selectedKbId} setSelectedKbId={setSelectedKbId} newKbName={newKbName} setNewKbName={setNewKbName} createKb={createKb} manageKnowledgeBase={manageKnowledgeBase} updateRetrievalConfig={updateRetrievalConfig} onContinue={() => switchView("documents")}/>}
      {activeView === "documents" && (
        <Documents selectedKb={selectedKb} documents={documents} documentTotal={documentTotal} documentOffset={documentOffset} setDocumentOffset={setDocumentOffset} documentSearch={documentSearch} setDocumentSearch={setDocumentSearch} documentStatusFilter={documentStatusFilter} setDocumentStatusFilter={setDocumentStatusFilter} documentTypeFilter={documentTypeFilter} setDocumentTypeFilter={documentTypeFilter} documentsLoading={documentsLoading} hasCompletedDocuments={hasCompletedDocuments} showDeletedDocuments={showDeletedDocuments} setShowDeletedDocuments={setShowDeletedDocuments} uploadFile={uploadFile} setUploadFile={setUploadFile} uploadTitle={uploadTitle} setUploadTitle={setUploadTitle} uploadDocumentType={uploadDocumentType} setUploadDocumentType={setUploadDocumentType} documentTemplates={documentTemplates} uploadTemplateId={uploadTemplateId} setUploadTemplateId={setUploadTemplateId} uploadMetadata={uploadMetadata} setUploadMetadata={setUploadMetadata} createDocumentTemplate={createDocumentTemplate} updateDocumentTemplate={updateDocumentTemplate} deactivateDocumentTemplate={deactivateDocumentTemplate} activateDocumentTemplate={activateDocumentTemplate} uploadDocument={uploadDocument} isUploading={isUploading} openDocument={openDocument} extractLegalMetadata={extractLegalMetadata} saveLegalMetadata={saveLegalMetadata} deleteLegalMetadata={deleteLegalMetadata} saveDocumentMetadata={saveDocumentMetadata} reprocessDocument={reprocessDocument} deleteDocument={deleteDocument} restoreDocument={restoreDocument} reindexEmbeddings={reindexEmbeddings} refreshDocuments={() => loadKbData(selectedKbId).catch(showError)} documentPreview={documentPreview} documentJobs={documentJobs} legalInstruments={legalInstruments} resolveLegalRegistry={resolveLegalRegistry} updateLegalInstrument={updateLegalInstrument} onClosePreview={() => setDocumentPreview(null)} onCreateKb={() => switchView("knowledge-bases")} onSearch={() => switchView("search")} onExplore={() => switchView("explore")}/>
      )}
      {activeView === "search" && (
        <SearchView selectedKb={selectedKb} documents={documents} completedDocuments={hasCompletedDocuments} query={query} setQuery={setQuery} queryAsOfDate={queryAsOfDate} setQueryAsOfDate={setQueryAsOfDate} queryIncludeHistorical={queryIncludeHistorical} setQueryIncludeHistorical={setQueryIncludeHistorical} runQuery={runQuery} isQuerying={isQuerying} queryResult={queryResult} submitFeedback={submitQueryFeedback} onDocuments={() => switchView("documents")} onOpenSource={document => { switchView("documents"); openDocument(document); }}/>
      )}
      {activeView === "explore" && (
        <ExploreView selectedKb={selectedKb} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
      )}
      {activeView === "access" && <AccessView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} loadAccess={loadAccess} createMcpToken={createMcpToken} rotateMcpToken={rotateMcpToken} changeTokenState={changeTokenState}/>}
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
  return <section className="trace-decision"><div className="trace-decision-row"><span>Intent</span><strong>{plan.intent || trace.intent || "retrieval"}</strong></div><div className="trace-decision-row"><span>Decision source</span><strong>{plan.planner_source || "rules"}{plan.policy_version ? ` · policy v${plan.policy_version}` : ""}</strong></div><div className="trace-decision-row"><span>Why this route</span><strong>{plan.rationale || "No planner rationale was recorded."}</strong></div><div className="trace-decision-row"><span>Selected channels</span><strong>{plan.channels?.length ? plan.channels.join(" → ") : "None"}</strong></div><div className="trace-decision-row"><span>Limits</span><strong>top {plan.max_sources || "—"} · graph depth {plan.graph_depth || "—"} · {plan.graph_scope || "none"} scope</strong></div>{plan.fallback_reason && <div className="trace-decision-warning">Deterministic fallback: {plan.fallback_reason}</div>}<p className="trace-safe-note">A channel marked “Skipped by plan” is an intentional decision, not a runtime failure.</p>{plan.channels?.includes("graph") && <p className="trace-safe-note">Neo4j note: it receives graph projections for exploration; this graph channel is served from PostgreSQL graph tables, so Neo4j appears only when a runtime path calls it.</p>}</section>;
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

const WORKFLOW_LABELS = {
  "knowledge-bases": "Knowledge Bases",
  documents: "Documents",
  search: "Search",
  explore: "Explore graph",
  access: "Access & MCP",
  logs: "Logging",
};
const isAdministrationView = view => view === "access" || view === "logs";
// Bound how far "Back" can retrace: enough to undo a few steps without the
// breadcrumb trail growing without limit as the user bounces between views.
const MAX_VIEW_TRAIL = 4;
const pushViewTrail = (current, view) => {
  const index = current.lastIndexOf(view);
  const next = index >= 0 ? current.slice(0, index + 1) : [...current, view];
  return next.length > MAX_VIEW_TRAIL ? next.slice(-MAX_VIEW_TRAIL) : next;
};

function WorkflowNavigation({activeView, selectedKb, hasCompletedDocuments, viewTrail, onNavigate, onBack, onNavigateNext}) {
  const breadcrumbViews = viewTrail.reduce((items, view) => {
    if (isAdministrationView(view) && items.at(-1) !== "administration") items.push("administration");
    items.push(view);
    return items;
  }, []);
  const previousView = viewTrail.length > 1 ? viewTrail[viewTrail.length - 2] : null;
  const previousLabel = previousView ? WORKFLOW_LABELS[previousView] : null;
  const nextView = activeView === "knowledge-bases" ? "documents"
    : activeView === "documents" && hasCompletedDocuments ? "search"
      : activeView === "search" && hasCompletedDocuments ? "explore"
        : activeView === "access" ? "logs" : null;
  const nextLabel = nextView ? WORKFLOW_LABELS[nextView] : null;
  const canNavigateNext = nextView && (nextView !== "documents" || Boolean(selectedKb)) && (nextView !== "search" && nextView !== "explore" || Boolean(selectedKb && hasCompletedDocuments));

  return <nav className="workflow-navigation" aria-label="Workflow navigation">
    <div className="workflow-navigation-inner">
      <div className="workflow-navigation-path">
        <button type="button" className="workflow-back" onClick={onBack} disabled={!previousView} aria-label={previousLabel ? `Back to ${previousLabel}` : "Back"}>
          <span aria-hidden="true">←</span> Back{previousLabel ? ` to ${previousLabel}` : ""}
        </button>
        <ol className="workflow-breadcrumbs">
          {breadcrumbViews.map((view, index) => <li key={`${view}-${index}`}>
            {index < breadcrumbViews.length - 1 && view !== "administration" ? <button type="button" onClick={() => onNavigate(view)}>{view === "administration" ? "Administration" : WORKFLOW_LABELS[view]}</button> : <span aria-current={index === breadcrumbViews.length - 1 ? "page" : undefined}>{view === "administration" ? "Administration" : WORKFLOW_LABELS[view]}</span>}
          </li>)}
        </ol>
        {selectedKb && activeView !== "knowledge-bases" && <span className="workflow-context" title={selectedKb.name}>{selectedKb.name}</span>}
      </div>
      {canNavigateNext && <button type="button" className="workflow-next" onClick={() => onNavigateNext(nextView)}>{nextLabel}<span aria-hidden="true">→</span></button>}
    </div>
  </nav>;
}
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

function MetadataFields({fields = [], values = {}, onChange, isDisabled = false}) {
  const setValue = (key, value) => onChange({...values, [key]: value});
  if (!fields.length) return null;
  return <div className="metadata-field-grid">{fields.map(field => {
    const value = values[field.key] ?? (field.field_type === "boolean" ? false : "");
    const label = field.required ? `${field.label} · Required` : field.label;
    if (field.field_type === "textarea") return <TextArea key={field.key} label={label} value={value} onChange={next => setValue(field.key, next)} rows={3} description={field.help_text} isDisabled={isDisabled}/>;
    if (field.field_type === "boolean") return <DesignSystemCheckbox key={field.key} label={field.label} checked={Boolean(value)} onChange={next => setValue(field.key, next)} isDisabled={isDisabled}/>;
    if (field.field_type === "select") return <DesignSystemSelect key={field.key} label={label} value={value} onChange={next => setValue(field.key, next)} options={[{value: "", label: "Select…"}, ...(field.options || []).map(option => ({value: option, label: option}))]} isDisabled={isDisabled} description={field.help_text}/>;
    if (field.field_type === "date") return <label className="metadata-native-field" key={field.key}><span>{label}</span><input type="date" value={value} onChange={event => setValue(field.key, event.target.value)} disabled={isDisabled}/>{field.help_text && <small>{field.help_text}</small>}</label>;
    return <TextInput key={field.key} label={label} value={String(value)} onChange={next => setValue(field.key, field.field_type === "number" && next !== "" ? Number(next) : next)} type={field.field_type === "number" ? "number" : "text"} description={field.help_text} isDisabled={isDisabled}/>;
  })}</div>;
}

function DocumentTypeEditor({draft, setDraft, editing, error, setError, onSubmit, onCancel, profileDefaults}) {
  const addField = () => setDraft(current => ({...current, fields: [...current.fields, {key: "", label: "", field_type: "text", required: false, help_text: "", options: [], searchable: true, filterable: false, graph_entity_type: "", graph_relationship: ""}]}));
  const copyProfileDefaults = () => setDraft(current => ({...current, fields: (profileDefaults[current.base_document_type] || []).map(field => ({...field}))}));
  const updateField = (index, patch) => setDraft(current => ({...current, fields: current.fields.map((field, currentIndex) => currentIndex === index ? {...field, ...patch} : field)}));
  const removeField = index => setDraft(current => ({...current, fields: current.fields.filter((_, currentIndex) => currentIndex !== index)}));
  return <form className="template-form" onSubmit={onSubmit}>
    <div className="drawer-form-heading"><div><p className="eyebrow">{editing ? "EDIT TYPE" : "NEW TYPE"}</p><h3>{editing ? "Edit document type" : "Create document type"}</h3></div><span className="section-copy">Profile controls processing; fields hold document metadata.</span></div>
    <TextInput label="Type name" value={draft.name} onChange={name => setDraft(current => ({...current, name}))} placeholder="e.g. Official notification" isRequired/>
    <TextInput label="Short description" value={draft.description} onChange={description => setDraft(current => ({...current, description}))} placeholder="What this type is used for" isOptional/>
    <DesignSystemSelect label="Processing profile" value={draft.base_document_type} onChange={base_document_type => setDraft(current => ({...current, base_document_type, fields: current.fields.length ? current.fields : (profileDefaults[base_document_type] || []).map(field => ({...field}))}))} options={DOCUMENT_TYPE_OPTIONS.map(option => ({value: option.value, label: option.label}))}/>
    <div className="template-field-builder"><div><b>Metadata fields</b><span className="template-field-actions"><Button label="Use profile defaults" type="button" size="sm" variant="ghost" onClick={copyProfileDefaults} isDisabled={!profileDefaults[draft.base_document_type]?.length}/><Button label="Add field" type="button" size="sm" variant="ghost" onClick={addField}/></span></div>{draft.fields.map((field, index) => <div className="template-field-row" key={`${field.key}-${index}`}>
      <div className="template-field-control"><TextInput label="Field key" value={field.key} onChange={key => updateField(index, {key})} placeholder="issuer"/></div>
      <div className="template-field-control"><TextInput label="Label" value={field.label} onChange={label => updateField(index, {label})} placeholder="Issuing organization"/></div>
      <div className="template-field-control"><DesignSystemSelect label="Field type" value={field.field_type} onChange={field_type => updateField(index, {field_type})} options={["text", "textarea", "date", "number", "select", "boolean"].map(value => ({value, label: value}))}/></div>
      <div className="template-field-control template-field-required"><DesignSystemCheckbox label="Required" checked={field.required} onChange={required => updateField(index, {required})}/></div>
      <div className="template-field-control template-field-help"><TextInput label="Help text" value={field.help_text || ""} onChange={help_text => updateField(index, {help_text})} placeholder="Optional guidance" isOptional/></div>
      {field.field_type === "select" && <div className="template-field-control template-field-options"><TextInput label="Options" value={(field.options || []).join(", ")} onChange={value => updateField(index, {options: value.split(",").map(item => item.trim()).filter(Boolean)})} placeholder="Option A, Option B"/></div>}
      <details className="template-field-advanced"><summary>Capabilities and graph mapping</summary><div className="template-field-capabilities"><DesignSystemCheckbox label="Search" checked={field.searchable !== false} onChange={searchable => updateField(index, {searchable})}/><DesignSystemCheckbox label="Filter" checked={Boolean(field.filterable)} onChange={filterable => updateField(index, {filterable})}/><DesignSystemCheckbox label="Graph" checked={Boolean(field.graph_relationship)} onChange={enabled => updateField(index, enabled ? {graph_entity_type: field.graph_entity_type || "Entity", graph_relationship: field.graph_relationship || "RELATED_TO"} : {graph_entity_type: "", graph_relationship: ""})}/></div>{field.graph_relationship && <div className="template-field-control template-field-graph"><TextInput label="Graph entity type" value={field.graph_entity_type || ""} onChange={graph_entity_type => updateField(index, {graph_entity_type})} placeholder="Organization"/><TextInput label="Relationship" value={field.graph_relationship || ""} onChange={graph_relationship => updateField(index, {graph_relationship: graph_relationship.toUpperCase().replace(/[^A-Z0-9_]/g, "")})} placeholder="ISSUED_BY"/></div>}</details>
      <div className="template-field-action"><Button label="Remove" type="button" size="sm" variant="destructive" onClick={() => removeField(index)}/></div>
    </div>)}</div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="preview-actions"><Button label={editing ? "Save document type" : "Create document type"} type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={onCancel}/></div>
  </form>;
}

function DocumentTypeDrawer({open, templates, onClose, onCreate, onUpdate, onDeactivate, onActivate}) {
  const emptyDraft = () => ({name: "", description: "", base_document_type: "general", fields: []});
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [draft, setDraft] = useState(emptyDraft);
  const [editing, setEditing] = useState(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const headingRef = useRef(null);
  const drawerRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    headingRef.current?.focus();
    const handleKeyDown = event => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...(drawerRef.current?.querySelectorAll("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])") || [])]
        .filter(element => element.getAttribute("aria-hidden") !== "true");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", handleKeyDown); document.body.style.overflow = previousOverflow; };
  }, [open, onClose]);
  if (!open) return null;
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filtered = templates.filter(template => {
    const matchesSearch = !normalizedSearch || `${template.name} ${template.description || ""} ${template.code}`.toLocaleLowerCase().includes(normalizedSearch);
    const matchesStatus = statusFilter === "all" || (statusFilter === "active" ? template.is_active !== false : template.is_active === false);
    return matchesSearch && matchesStatus;
  });
  const systemTemplates = filtered.filter(template => template.is_system);
  const customTemplates = filtered.filter(template => !template.is_system);
  const profileDefaults = Object.fromEntries(templates.filter(template => template.is_system).map(template => [template.base_document_type, template.fields || []]));
  const resetEditor = () => { setEditing(null); setCreating(false); setDraft(emptyDraft()); setError(""); };
  const startCreate = () => { setEditing(null); setCreating(true); setDraft(emptyDraft()); setError(""); };
  const startEdit = template => { setEditing(template); setCreating(false); setDraft({name: template.name, description: template.description || "", base_document_type: template.base_document_type, fields: template.fields || []}); setError(""); };
  const submit = async event => {
    event.preventDefault();
    if (!draft.name.trim()) { setError("Enter a document type name."); return; }
    if (draft.fields.some(field => !/^[a-z][a-z0-9_]*$/.test(field.key) || !field.label.trim())) { setError("Each field needs a lowercase key and a label."); return; }
    if (new Set(draft.fields.map(field => field.key)).size !== draft.fields.length) { setError("Field keys must be unique."); return; }
    try { if (editing) await onUpdate(editing.id, draft); else await onCreate(draft); resetEditor(); }
    catch (requestError) { setError(requestError.message || "Unable to save this document type."); }
  };
  const renderRow = template => <article className="document-type-row" key={template.id}>
    <div className="document-type-row-main"><div className="document-type-row-title"><b>{template.name}</b><span className={`template-status ${template.is_active === false ? "inactive" : "active"}`}>{template.is_active === false ? "Inactive" : "Active"}</span>{template.is_system && <span className="template-system-badge">Built-in</span>}</div><p>{template.description || "No description"}</p><small>{template.base_document_type} · {template.fields.length} field{template.fields.length === 1 ? "" : "s"} · {template.usage_count || 0} document{template.usage_count === 1 ? "" : "s"} · v{template.version}</small></div>
    {!template.is_system && <div className="document-type-row-actions"><Button label="Edit" size="sm" variant="ghost" onClick={() => startEdit(template)}/>{template.is_active === false ? <Button label="Restore" size="sm" variant="secondary" onClick={() => onActivate(template)}/> : <Button label="Archive" size="sm" variant="ghost" onClick={() => onDeactivate(template)}/>}</div>}
  </article>;
  return <div className="document-type-drawer-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside ref={drawerRef} className="document-type-drawer" role="dialog" aria-modal="true" aria-labelledby="document-type-drawer-title" onMouseDown={event => event.stopPropagation()}>
    <header className="document-type-drawer-header"><div><p className="eyebrow">DOCUMENT TYPES</p><h2 id="document-type-drawer-title" tabIndex={-1} ref={headingRef}>Manage document types</h2><p>{templates.length} types in this Knowledge Base</p></div><button type="button" className="drawer-close" onClick={onClose} aria-label="Close document type manager">×</button></header>
    <div className="document-type-controls"><TextInput label="Find a type" value={search} onChange={setSearch} placeholder="Name, description, or code"/><DesignSystemSelect label="Status" value={statusFilter} onChange={setStatusFilter} options={[{value: "all", label: "All statuses"}, {value: "active", label: "Active"}, {value: "inactive", label: "Inactive"}]}/></div>
    <div className="document-type-drawer-actions"><Button label={creating || editing ? "Cancel editing" : "Create document type"} size="sm" variant="primary" onClick={() => (creating || editing) ? resetEditor() : startCreate()}/></div>
    {(creating || editing) && <DocumentTypeEditor draft={draft} setDraft={setDraft} editing={editing} error={error} setError={setError} onSubmit={submit} onCancel={resetEditor} profileDefaults={profileDefaults}/>}
    <section className="document-type-section"><div className="document-type-section-heading"><h3>Built-in types</h3><span>{systemTemplates.length}</span></div>{systemTemplates.length ? systemTemplates.map(renderRow) : <p className="document-type-empty">No built-in types match this filter.</p>}</section>
    <section className="document-type-section"><div className="document-type-section-heading"><h3>Custom types</h3><span>{customTemplates.length}</span></div>{customTemplates.length ? customTemplates.map(renderRow) : <p className="document-type-empty">No custom types yet. Create one to collect repeatable metadata.</p>}</section>
  </aside></div>;
}

function Documents({selectedKb, documents, documentTotal, documentOffset, setDocumentOffset, documentSearch, setDocumentSearch, documentStatusFilter, setDocumentStatusFilter, documentTypeFilter, setDocumentTypeFilter, documentsLoading, hasCompletedDocuments, showDeletedDocuments, setShowDeletedDocuments, uploadFile, setUploadFile, uploadTitle, setUploadTitle, uploadDocumentType, setUploadDocumentType, documentTemplates, uploadTemplateId, setUploadTemplateId, uploadMetadata, setUploadMetadata, createDocumentTemplate, updateDocumentTemplate, deactivateDocumentTemplate, activateDocumentTemplate, uploadDocument, isUploading, openDocument, extractLegalMetadata, saveLegalMetadata, deleteLegalMetadata, saveDocumentMetadata, reprocessDocument, deleteDocument, restoreDocument, reindexEmbeddings, refreshDocuments, documentPreview, documentJobs, legalInstruments, resolveLegalRegistry, updateLegalInstrument, onClosePreview, onCreateKb, onSearch, onExplore}) {
  const [isTypeDrawerOpen, setIsTypeDrawerOpen] = useState(false);
  const typeManagerTriggerRef = useRef(null);
  const documentTriggerRef = useRef(null);
  const closeTypeDrawer = useCallback(() => {
    setIsTypeDrawerOpen(false);
    window.requestAnimationFrame(() => typeManagerTriggerRef.current?.focus());
  }, []);
  const openDocumentFromLibrary = (document, event) => {
    documentTriggerRef.current = event?.currentTarget || null;
    openDocument(document);
  };
  const closeDocumentPreview = () => {
    onClosePreview();
    window.requestAnimationFrame(() => documentTriggerRef.current?.focus());
  };
  if (!selectedKb) return <EmptyState title="Create a Knowledge Base first" description="Documents need a context so search results remain relevant and secure." actions={<Button label="Create Knowledge Base" variant="primary" onClick={onCreateKb}/>}/>;
  const pageSize = 50;
  const pageStart = documentTotal ? documentOffset + 1 : 0;
  const pageEnd = Math.min(documentOffset + documents.length, documentTotal);
  const hasPrevious = documentOffset > 0;
  const hasNext = documentOffset + documents.length < documentTotal;
  const processingStatus = document => document.processing_job_status && ["queued", "running"].includes(document.processing_job_status) ? document.processing_job_status : document.status;
  const activeTemplates = documentTemplates.filter(template => template.is_active !== false);
  const uploadTemplate = activeTemplates.find(template => template.id === uploadTemplateId) || activeTemplates[0] || {id: "system:general", name: "General document", base_document_type: uploadDocumentType, fields: [], description: "Search, citations, and knowledge graph."};
  return <><PageHeading eyebrow="DOCUMENTS" title={`Build ${selectedKb.name}`} description="Manage source files, processing status, and legal metadata in one place." actions={<><Button ref={typeManagerTriggerRef} label="Manage document types" variant="secondary" onClick={() => setIsTypeDrawerOpen(true)}/><Button label={showDeletedDocuments ? "Hide deleted" : "Show deleted"} variant="ghost" onClick={() => { setDocumentOffset(0); setShowDeletedDocuments(value => !value); }}/><Button label="Reindex embeddings" variant="secondary" onClick={reindexEmbeddings}/><Button label="Refresh status" variant="ghost" onClick={refreshDocuments}/></>}/>
    <Card padding={4} variant="muted"><form className="upload-layout" onSubmit={uploadDocument}><FileInput label="Add documents" value={uploadFile} onChange={files => setUploadFile(Array.isArray(files) ? files : files ? [files] : [])} isMultiple maxFiles={20} accept={ACCEPTED_FILES} maxSize={MAX_FILE_SIZE} mode="dropzone" description={`Select up to 20 files · PDF, Word, PowerPoint, Excel, TXT, Markdown, HTML, CSV, or JSON · up to ${MAX_FILE_SIZE_MB} MB each`} isLoading={isUploading}/><div className="upload-meta"><DesignSystemSelect label="Document type" value={uploadTemplate.id} onChange={templateId => { const next = activeTemplates.find(template => template.id === templateId); setUploadTemplateId(templateId); setUploadDocumentType(next?.base_document_type || "general"); setUploadMetadata({}); }} options={activeTemplates.map(template => ({value: template.id, label: template.name}))} isDisabled={isUploading} size="md"/><p className="section-copy document-type-help">{uploadTemplate.description} · applies to every selected file</p><MetadataFields fields={uploadTemplate.fields} values={uploadMetadata} onChange={setUploadMetadata} isDisabled={isUploading}/><TextInput label="Document title" value={uploadTitle} onChange={setUploadTitle} placeholder="Optional display title (single file only)" isOptional isDisabled={uploadFile.length !== 1 || isUploading}/>{uploadFile.length > 1 && <p className="section-copy document-type-help">Batch upload uses each original filename as its document title.</p>}<Button label={uploadFile.length > 1 ? `Upload ${uploadFile.length} files and process` : "Upload and process"} type="submit" variant="primary" isDisabled={!uploadFile.length} isLoading={isUploading}/></div></form><p className="section-copy upload-format-note">Each file becomes its own processing job. Failed files can be retried individually.</p></Card>
    <section className="content-section"><div className="section-title"><div><h2>{showDeletedDocuments ? "All documents" : "Library"}</h2><p>{documentTotal ? `Showing ${pageStart}–${pageEnd} of ${documentTotal} document${documentTotal === 1 ? "" : "s"}` : "Your uploaded documents will appear here."}</p></div>{documents.some(document => ["queued", "extracting", "indexing"].includes(document.status) || ["queued", "running"].includes(document.processing_job_status)) && <span className="live-status" role="status">Updating automatically</span>}</div>
      <div className="document-filter-bar"><TextInput label="Find a document" value={documentSearch} onChange={value => { setDocumentOffset(0); setDocumentSearch(value); }} placeholder="Title or filename"/><DesignSystemSelect label="Status" value={documentStatusFilter} onChange={value => { setDocumentOffset(0); setDocumentStatusFilter(value); }} options={[{value: "all", label: "All statuses"}, {value: "queued", label: "Queued"}, {value: "extracting", label: "Extracting"}, {value: "indexing", label: "Indexing"}, {value: "completed", label: "Ready"}, {value: "failed", label: "Needs attention"}, {value: "ocr_required", label: "OCR required"}, {value: "deleted", label: "Deleted"}]}/><DesignSystemSelect label="Document type" value={documentTypeFilter} onChange={value => { setDocumentOffset(0); setDocumentTypeFilter(value); }} options={[{value: "all", label: "All types"}, ...DOCUMENT_TYPE_OPTIONS.map(option => ({value: option.value, label: option.label})), ...documentTemplates.filter(template => !template.is_system).map(template => ({value: template.id, label: template.name}))]}/></div>
    {documentsLoading && !documents.length ? <p className="section-copy" role="status">Loading documents…</p> : documents.length ? <div className="document-table">{documents.map(document => { const activeStatus = processingStatus(document); const processing = ["queued", "extracting", "indexing"].includes(document.status) || ["queued", "running"].includes(document.processing_job_status); const failed = ["failed", "ocr_required"].includes(document.status) || document.processing_job_status === "failed"; return <article key={document.id} className="document-item"><div className="document-main"><button type="button" className="document-title" onClick={event => openDocumentFromLibrary(document, event)}>{document.title || document.original_filename}</button><p>{document.original_filename} · {Math.ceil(document.file_size / 1024)} KB · {document.metadata_template_name || documentTypeLabel(document.document_type)}</p>{processing && <><ProgressBar label={`${document.title || document.original_filename} processing`} value={document.processing_job_progress_percent ?? 0} variant="warning" isIndeterminate={document.processing_job_progress_percent == null}/><p className="document-status-help">{STATUS_HELP[activeStatus] || document.processing_job_stage || "Processing document…"}</p></>}{failed && <p className="document-status-help document-status-warning">{STATUS_HELP[document.status] || "Processing stopped. Open the document to see the reason."}{document.error_code ? ` (${document.error_code})` : ""}</p>}</div><StatusBadge status={document.status}/><div className="document-actions"><Button label="Open details" variant="ghost" size="sm" onClick={event => openDocumentFromLibrary(document, event)}/>{document.deleted_at ? <Button label="Restore" variant="secondary" size="sm" onClick={() => restoreDocument(document)}/> : <><Button label="Process again" variant="secondary" size="sm" isDisabled={processing} onClick={() => reprocessDocument(document)}/><Button label="Delete" variant="destructive" size="sm" onClick={() => deleteDocument(document)}/></>}</div></article>; })}</div> : <EmptyState title={documentTotal ? "No matching documents" : "Your library is ready for its first document"} description={documentTotal ? "Try another search or filter." : "Use the drop zone above to add a document."}/>}
      {documentTotal > pageSize && <div className="document-pagination"><Button label="Previous" variant="ghost" size="sm" isDisabled={!hasPrevious || documentsLoading} onClick={() => setDocumentOffset(Math.max(0, documentOffset - pageSize))}/><span>{pageStart}–{pageEnd} / {documentTotal}</span><Button label="Next" variant="secondary" size="sm" isDisabled={!hasNext || documentsLoading} onClick={() => setDocumentOffset(documentOffset + pageSize)}/></div>}
    </section>
    {hasCompletedDocuments && <section className="next-step-card"><div><p className="eyebrow">NEXT STEP</p><h2>Your knowledge is ready to use</h2><p>Ask a question for cited answers, or explore the entities and relationships found in your documents.</p></div><div className="next-step-actions"><Button label="Search knowledge" variant="primary" onClick={onSearch}/><Button label="Explore graph" variant="secondary" onClick={onExplore}/></div></section>}
    {legalInstruments?.length > 0 && <LegalRegistryPanel instruments={legalInstruments} resolveLegalRegistry={resolveLegalRegistry} onOpenDocument={openDocumentFromLibrary}/>}
    {documentPreview && <DocumentPreview preview={documentPreview} jobs={documentJobs} templates={documentTemplates} legalInstrument={legalInstruments?.find(row => row.document_id === documentPreview.document_id)} onExtractLegal={extractLegalMetadata} onSaveLegal={saveLegalMetadata} onDeleteLegal={deleteLegalMetadata} onSaveDocumentMetadata={saveDocumentMetadata} onUpdateLegalInstrument={updateLegalInstrument} onClose={closeDocumentPreview}/>}<DocumentTypeDrawer open={isTypeDrawerOpen} templates={documentTemplates} onClose={closeTypeDrawer} onCreate={createDocumentTemplate} onUpdate={updateDocumentTemplate} onDeactivate={deactivateDocumentTemplate} onActivate={activateDocumentTemplate}/></>
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

const LEGAL_ENTITY_LABELS_TH = {
  LegalInstrument: "ตราสารกฎหมาย", Provision: "มาตรา/ข้อ", LegalAuthority: "ผู้มีอำนาจตามกฎหมาย",
  LegalParty: "คู่กรณี/คู่สัญญา", Obligation: "หน้าที่", Right: "สิทธิ", Prohibition: "ข้อห้าม",
  Penalty: "บทลงโทษ", Definition: "คำนิยาม", Amendment: "การแก้ไขเพิ่มเติม",
};
const LEGAL_RELATIONSHIP_LABELS_TH = {
  CONTAINS_PROVISION: "มีมาตรา/ข้อ", DEFINES: "ให้นิยาม", ISSUED_BY: "ออกโดย",
  ISSUED_UNDER: "ออกตามอำนาจของ", AMENDS: "แก้ไขเพิ่มเติม", REPEALS: "ยกเลิก",
  IMPLEMENTS: "กำหนดแนวทางปฏิบัติตาม", REFERS_TO: "อ้างถึง", GOVERNED_BY: "อยู่ภายใต้บังคับของ",
  REQUIRES: "กำหนดให้ต้อง", GRANTS_RIGHT: "ให้สิทธิ", PROHIBITS: "ห้าม", PARTY_TO: "เป็นคู่สัญญาของ",
  SUPERSEDES: "แทนที่ฉบับเดิม", RELATED_TO: "เกี่ยวข้องกับ", DEPENDS_ON: "พึ่งพา", RUNS_ON: "ทำงานบน",
  USES: "ใช้", SUPPORTS: "รองรับ", AFFECTS: "ส่งผลต่อ", CONNECTS_TO: "เชื่อมต่อกับ",
};
const REVIEW_STATUS_LABELS_TH = {
  verified: "ยืนยันแล้ว", suggested: "แนะนำจากระบบ · รอตรวจสอบ", rejected: "ถูกปฏิเสธ",
  unreviewed: "ยังไม่ตรวจทาน",
};
const RELATIONSHIP_ORIGIN_LABELS_TH = {
  legal_schema: "สกัดตามโครงสร้างกฎหมาย", ai_suggestion: "แนะนำโดย AI", manual: "สร้างโดยผู้ดูแล",
};

const legalStatusLabel = status => LEGAL_STATUS_LABELS_TH[status] || "ไม่ทราบสถานะ";
const legalClassLabel = instrument => LEGAL_CLASS_LABELS_TH[instrument.document_class] || LEGAL_KIND_LABELS_TH[instrument.kind] || "ตราสารกฎหมาย";
const legalEntityLabel = type => LEGAL_ENTITY_LABELS_TH[type] || type || "เอนทิตี";
const relationshipLabel = type => LEGAL_RELATIONSHIP_LABELS_TH[type] || String(type || "").replace(/_/g, " ");
const reviewStatusLabel = status => REVIEW_STATUS_LABELS_TH[status] || REVIEW_STATUS_LABELS_TH.unreviewed;
const relationshipOriginLabel = origin => RELATIONSHIP_ORIGIN_LABELS_TH[origin] || String(origin || "ไม่ระบุ").replace(/_/g, " ");
const reviewBadgeVariant = status => ({verified: "success", suggested: "warning", rejected: "error"}[status] || "neutral");
const legalDateValue = instrument => instrument.effective_from || instrument.version_date || "";
const legalDateLabel = instrument => {
  if (!instrument.effective_from) return instrument.version_date ? `ฉบับวันที่ ${instrument.version_date}` : "ยังไม่ระบุวันที่";
  return `มีผล ${instrument.effective_from}`;
};

function LegalRegistryPanel({instruments, resolveLegalRegistry, onOpenDocument}) {
  return <section className="content-section legal-registry-panel"><div className="section-title"><div><h2>Legal registry</h2><p>{instruments.length} ตราสารกฎหมายที่ติดตามสถานะ แหล่งที่มา และวันที่มีผล</p></div><Button label="Resolve statuses" variant="secondary" size="sm" onClick={resolveLegalRegistry}/></div>
    <div className="document-table">{instruments.map(row => <article key={row.id} className="document-item legal-registry-row"><div className="document-main"><button type="button" className="document-title" onClick={event => row.document_id && onOpenDocument({id: row.document_id, title: row.official_title || row.document_id}, event)}>{row.official_title || row.document_id}</button><p>{LEGAL_KIND_LABELS_TH[row.kind] || row.kind} · ระดับอำนาจ {row.authority_level}{row.version_label ? ` · ${row.version_label}` : ""}{row.effective_from ? ` · มีผล ${row.effective_from}` : ""}{row.effective_to ? ` ถึง ${row.effective_to}` : ""}</p><p className="section-copy">{reviewStatusLabel(row.review_status)} · แหล่งที่มา: {row.source_reference || row.source_uri || "ยังไม่ระบุ"}</p>{row.status_reason && <p className="section-copy">{row.status_reason}</p>}</div><LegalStatusBadge status={row.status}/>
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
  const description = isLegalGraph ? "ระบบแสดงโครงสร้างกฎหมายที่ยืนยันแล้วเป็นค่าเริ่มต้น ส่วนความสัมพันธ์ข้ามเอกสารที่ระบบแนะนำจะแสดงหลักฐานให้ตรวจสอบก่อนอนุมัติ" : "คลิกพื้นที่ว่างเพื่อเพิ่มโหนด แล้วลากจากขอบโหนดหนึ่งไปยังอีกโหนดเพื่อเชื่อมความสัมพันธ์";
  return <><PageHeading eyebrow="EXPLORE" title={isLegalGraph ? "Explore your legal knowledge graph" : "Explore your knowledge graph"} description={description}/>
    <GraphWorkspace knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship}/>
  </>;
}

const ENTITY_TYPES = ["Application", "Service", "Server", "Database", "BusinessProcess", "Organization", "Concept"];
const LEGAL_ENTITY_TYPES = ["LegalInstrument", "Provision", "LegalAuthority", "LegalParty", "Obligation", "Right", "Prohibition", "Penalty", "Definition", "Amendment"];
const RELATIONSHIP_TYPES = ["DEPENDS_ON", "RUNS_ON", "USES", "SUPPORTS", "AFFECTS"];

function KnowledgeNode({data, selected}) {
  const isPerson = /person|people|organization|user|team/i.test(data.entityType);
  const isLegal = Boolean(data.isLegal || LEGAL_ENTITY_LABELS_TH[data.entityType]);
  const reviewStatus = data.reviewStatus || "unreviewed";
  const handles = [
    ["top", Position.Top],
    ["right", Position.Right],
    ["bottom", Position.Bottom],
    ["left", Position.Left],
  ];
  return <div className={`knowledge-node graph-visual-node ${isPerson ? "person-node" : "asset-node"} ${selected ? "selected" : ""}`}>
    {handles.map(([id, position]) => <Handle key={id} id={id} type="source" position={position} className={`graph-handle graph-handle-${id}`}/>) }
    <div className="graph-node-circle" title={data.entityType}>{isPerson ? <span className="person-glyph"><i/><b/></span> : <span className="asset-glyph"><i/><i/><i/><i/><i/><i/></span>}</div>
    <strong className="graph-node-label" title={data.label}>{data.label}</strong>
    <span className="graph-node-type" title={isLegal ? legalEntityLabel(data.entityType) : data.entityType}>{isLegal ? legalEntityLabel(data.entityType) : data.entityType}{data.documentId ? ` · ${String(data.documentId).slice(0, 8)}` : ""}</span>
    {isLegal && <span className={`graph-node-review ${reviewStatus}`}><i aria-hidden="true"/>{reviewStatusLabel(reviewStatus)}</span>}
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
  const relationshipSummary = map?.relationship_summary || {};
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
    <section className="legal-review-guide" aria-label="คำอธิบายสถานะความสัมพันธ์">
      <div><b>สถานะความสัมพันธ์</b><span>สถานะของความสัมพันธ์แยกจากสถานะมีผลใช้บังคับของเอกสาร</span></div>
      <div className="legal-review-legend">
        <span><i className="verified" aria-hidden="true"/>ยืนยันแล้ว <b>{relationshipSummary.verified ?? relationCount}</b></span>
        <span><i className="suggested" aria-hidden="true"/>แนะนำจากระบบ · รอตรวจสอบ <b>{relationshipSummary.suggested ?? 0}</b></span>
        <span><i className="manual" aria-hidden="true"/>สร้างโดยผู้ดูแล <b>{relationshipSummary.manual ?? 0}</b></span>
        <span className="legal-review-scope">ภายในเอกสาร {relationshipSummary.internal ?? relationCount} · ข้ามเอกสาร {relationshipSummary.cross_document ?? 0}</span>
      </div>
    </section>
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
  const statusVariant = reviewBadgeVariant(legal.review_status);
  return <div className="legal-inspector">
    <p className="eyebrow">ข้อมูลโหนดกฎหมายที่เลือก</p><h2>{legal.name}</h2>
    <div className="inspector-badges"><Badge label={legalEntityLabel(legal.entity_type)} variant="info"/><Badge label={reviewStatusLabel(legal.review_status)} variant={statusVariant}/>{legal.origin && <Badge label={relationshipOriginLabel(legal.origin)} variant="neutral"/>}</div>
    <div className="inspector-trust-note"><b>{legal.review_status === "suggested" ? "ยังไม่ถือเป็นข้อเท็จจริง" : "ใช้เป็นโครงสร้างอ้างอิงได้"}</b><span>{legal.review_status === "suggested" ? "ตรวจสอบหลักฐานก่อนนำไปใช้ตอบคำถามหรืออนุมัติความสัมพันธ์" : "มีหลักฐานจากเอกสารต้นทางหรือได้รับการตรวจทานแล้ว"}</span></div>
    <div className="inspector-tabs" role="tablist">{[["overview","ภาพรวม"],["evidence","หลักฐาน"],["relations","ความสัมพันธ์"],["versions","ฉบับ/ประวัติ"]].map(([value,label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}</button>)}</div>
    {loading && <p className="section-copy" role="status">กำลังโหลดบริบทกฎหมาย…</p>}
    {!loading && tab === "overview" && <div className="inspector-section">
      <dl className="inspector-meta"><div><dt>รหัสอ้างอิง</dt><dd><code>{legal.identity_key || legal.id}</code></dd></div><div><dt>ความเชื่อมั่น</dt><dd>{legal.confidence == null ? "ไม่ระบุ" : `${Math.round(legal.confidence * 100)}%`}</dd></div><div><dt>แหล่งหลักฐาน</dt><dd>{legal.source_count ?? evidence.length} รายการ</dd></div></dl>
      {context.documents?.map(document => <div className="inspector-context-card" key={document.document_id}><b>{document.title}</b><span>{document.document_type} · {document.status}</span>{document.instrument && <span>{LEGAL_KIND_LABELS_TH[document.instrument.kind] || document.instrument.kind} · {legalStatusLabel(document.instrument.status)}{document.instrument.version_label ? ` · ${document.instrument.version_label}` : ""}</span>}</div>)}
      {!context.documents?.length && <p className="section-copy">ยังไม่มีข้อมูลเอกสารต้นทางที่เชื่อมกับโหนดนี้</p>}
      {warnings.map(warning => <p className="inline-error" key={warning}>⚠ {warning}</p>)}
      <div className="preview-actions"><Button label="ดูโหนดที่เชื่อมโยง" size="sm" variant="secondary" onClick={() => onFocus(1)}/><Button label="วิเคราะห์ผลกระทบ" size="sm" variant="secondary" onClick={onImpact}/></div>
    </div>}
    {!loading && tab === "evidence" && <div className="inspector-section">{evidence.length ? evidence.map((source, index) => <details className="inspector-evidence" open={index === 0} key={`${source.document_id}-${index}`}><summary>{source.title}</summary><p>{source.excerpt || "ไม่พบข้อความหลักฐาน"}</p></details>) : <p className="section-copy">ยังไม่มีข้อความหลักฐานสำหรับโหนดนี้</p>}</div>}
    {!loading && tab === "relations" && <div className="inspector-section">{[...incoming, ...outgoing].length ? <ul className="inspector-relations">{[...incoming, ...outgoing].map(relation => <li key={relation.id}><b>{relation.direction === "incoming" ? "←" : "→"} {relationshipLabel(relation.relationship_type)}</b><span>{relation.other_entity?.name || "ไม่ทราบโหนด"} · {reviewStatusLabel(relation.review_status)}</span><small>{relationshipOriginLabel(relation.origin)}{relation.confidence == null ? "" : ` · ความเชื่อมั่น ${Math.round(relation.confidence * 100)}%`}</small>{relation.sources?.[0]?.excerpt && <small>{relation.sources[0].excerpt}</small>}</li>)}</ul> : <p className="section-copy">ไม่พบความสัมพันธ์ที่ยืนยันแล้วหรือสร้างโดยผู้ดูแล</p>}</div>}
    {!loading && tab === "versions" && <div className="inspector-section">{versions.length ? versions.map(version => <div className="inspector-context-card" key={version.id}><b>{version.official_title || version.document_id}</b><span>{LEGAL_KIND_LABELS_TH[version.kind] || version.kind} · {legalStatusLabel(version.status)} · {version.effective_from || "ไม่ระบุวันที่"}</span></div>) : <p className="section-copy">ยังไม่มีประวัติฉบับหรือสายกฎหมายที่เชื่อมโยง</p>}{data?.versions?.relations?.map(relation => <p className="section-copy" key={relation.id}><b>{relationshipLabel(relation.relation)}</b> · {reviewStatusLabel(relation.review_status)}{relation.evidence_quote ? ` · ${relation.evidence_quote}` : ""}</p>)}</div>}
    <p className="section-copy graph-help">โหนดกฎหมายเป็นข้อมูลอ่านอย่างเดียวและสร้างจาก metadata ที่มีแหล่งอ้างอิง ควรตรวจสอบความสัมพันธ์ที่ระบบแนะนำก่อนถือเป็นข้อเท็จจริง</p>
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
    const autoArrangeDenseLegal = isLegalGraph && entities.length > 10;
    const visible = entities.filter(entity => (!graphSearch.trim() || `${entity.name} ${entity.entity_type}`.toLowerCase().includes(graphSearch.trim().toLowerCase())) && (graphTypeFilter === "all" || entity.entity_type === graphTypeFilter) && (graphStatusFilter === "all" || entity.review_status === graphStatusFilter));
    setNodes(current => visible.map((entity, index) => {
      const existing = current.find(node => node.id === entity.id);
      const rank = rankById[entity.id] ?? index;
      // Keep the highest-degree node in the centre and distribute the rest
      // across multiple rings. A single tight ring works for small graphs but
      // causes Thai labels and edge endpoints to overlap as legal instruments
      // grow beyond a handful of provisions.
      const outerIndex = Math.max(0, rank - 1);
      const slotsPerRing = 8;
      const ringIndex = Math.floor(outerIndex / slotsPerRing);
      const slotIndex = outerIndex % slotsPerRing;
      const slotCount = Math.min(slotsPerRing, Math.max(1, entities.length - 1 - ringIndex * slotsPerRing));
      const angle = ((slotIndex / slotCount) * Math.PI * 2) - Math.PI / 2;
      const radius = 300 + ringIndex * 190;
      const automaticPosition = rank === 0 ? {x: 640, y: 480} : {x: 640 + Math.cos(angle) * radius, y: 480 + Math.sin(angle) * radius};
      return {id: entity.id, type: "knowledge", position: existing?.position || (!autoArrangeDenseLegal && layout[entity.id]) || automaticPosition, data: {label: entity.name, entityType: entity.entity_type, documentId: entity.attributes?.document_id, reviewStatus: entity.review_status, isLegal: entity.is_legal}};
    }));
  }, [entities, relationships, layout, setNodes, graphSearch, graphTypeFilter, graphStatusFilter, isLegalGraph]);

  useEffect(() => {
    const nodesById = Object.fromEntries(nodes.map(node => [node.id, node]));
    setEdges(relationships.filter(relationship => nodesById[relationship.source_entity_id] && nodesById[relationship.target_entity_id]).map(relationship => {
      const isDependency = /DEPEND|RUNS_ON|USES/i.test(relationship.relationship_type);
      const reviewStatus = relationship.review_status || "verified";
      const statusColor = reviewStatus === "suggested" ? "#d58b14" : reviewStatus === "rejected" ? "#9a6a6a" : relationship.origin === "manual" ? "#65439a" : (isDependency ? "#56328d" : "#008c96");
      const handles = connectionHandles(nodesById[relationship.source_entity_id], nodesById[relationship.target_entity_id]);
      const showLabel = !isLegalGraph || relationships.length <= 8 || relationship.id === selectedRelationshipId;
      return {id: relationship.id, source: relationship.source_entity_id, target: relationship.target_entity_id, ...handles, label: showLabel ? relationshipLabel(relationship.relationship_type) : undefined, type: "straight", markerEnd: {type: MarkerType.ArrowClosed, color: statusColor}, style: {stroke: statusColor, strokeWidth: reviewStatus === "suggested" ? 2.2 : 1.8, strokeDasharray: reviewStatus === "suggested" ? "7 5" : reviewStatus === "rejected" ? "3 5" : undefined, opacity: reviewStatus === "rejected" ? .55 : 1}, labelStyle: {fill: statusColor, fontWeight: 700, fontSize: 11}, labelBgStyle: {fill: "#ffffff", fillOpacity: 0.96}};
    }));
  }, [nodes, relationships, selectedRelationshipId, isLegalGraph, setEdges]);

  useEffect(() => { if (entities.length) requestAnimationFrame(() => fitView({padding: 0.3, duration: 280})); }, [entities.length, fitView]);

  const selectedEntity = entities.find(entity => entity.id === selectedEntityId);
  const selectedRelationship = relationships.find(relationship => relationship.id === selectedRelationshipId);
  const entityNamesById = useMemo(() => Object.fromEntries(entities.map(entity => [entity.id, entity.name])), [entities]);
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
  const selectedEntityPanel = isLegalGraph && legalGraphView !== "manual" ? <LegalInspector entity={selectedEntity} data={inspectorData} loading={inspectorLoading} tab={inspectorTab} setTab={setInspectorTab} onImpact={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})} onFocus={focusSelected}/> : <form className="form-stack" onSubmit={updateSelectedEntity}><p className="eyebrow">SELECTED ENTITY</p><h2>Edit entity</h2><TextInput label="Entity name" value={editName} onChange={setEditName} isRequired/><DesignSystemSelect label="Type" value={editEntityType} onChange={setEditEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: type}))} size="md"/><p className="section-copy graph-help">ลากจากขอบโหนดไปยังโหนดอื่นเพื่อสร้างความสัมพันธ์ “{relationshipLabel(relationshipType)}”</p><Button label="Save entity" type="submit" variant="primary" isDisabled={!editName.trim()}/><div className="form-stack graph-impact-form"><TextInput label="Impact scenario" value={scenario} onChange={setScenario} placeholder="e.g. stops working" isRequired/><Button label="Analyze impact" type="button" variant="secondary" onClick={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})}/></div><Button label="Delete entity" type="button" variant="destructive" onClick={deleteSelectedEntity}/></form>;
  return <section className="graph-workspace"><div className="graph-toolbar"><div><Badge label={`${entities.length} โหนด`} variant="info"/><Badge label={`${relationships.length} ความสัมพันธ์`} variant="neutral"/></div>{isLegalGraph ? <><DesignSystemSelect label="มุมมองกราฟ" value={legalGraphView} onChange={setLegalGraphView} options={[{value: "verified", label: "โครงสร้างที่ยืนยันแล้ว"}, {value: "suggested", label: "ความสัมพันธ์ที่ระบบแนะนำ"}, {value: "manual", label: "กราฟที่ผู้ดูแลสร้าง"}, {value: "all", label: "หลักฐานกราฟทั้งหมด"}]}/><label className="relationship-picker">ค้นหาโหนด<input value={graphSearch} onChange={event => setGraphSearch(event.target.value)} placeholder="ชื่อโหนดหรือประเภท"/></label><DesignSystemSelect label="ประเภทโหนด" value={graphTypeFilter} onChange={setGraphTypeFilter} options={[{value: "all", label: "ทุกประเภท"}, ...legalTypes.map(type => ({value: type, label: legalEntityLabel(type)}))]}/><DesignSystemSelect label="สถานะตรวจทาน" value={graphStatusFilter} onChange={setGraphStatusFilter} options={[{value: "all", label: "ทุกสถานะ"}, {value: "verified", label: "ยืนยันแล้ว"}, {value: "suggested", label: "แนะนำ · รอตรวจสอบ"}, {value: "rejected", label: "ถูกปฏิเสธ"}]}/><Button label={legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status) ? "กำลังสร้างกราฟกฎหมาย…" : "สร้างกราฟกฎหมายใหม่"} variant="secondary" size="sm" onClick={queueLegalGraphRebuild} isDisabled={Boolean(legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status))}/></> : <><DesignSystemSelect label="ประเภทความสัมพันธ์ใหม่" value={relationshipType} onChange={setRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: relationshipLabel(type)}))}/><Button label="นำเข้าจากเอกสาร" variant="secondary" size="sm" onClick={syncGraph}/></>}<Button label="จัดกราฟให้พอดี" variant="ghost" size="sm" onClick={() => fitView({padding: 0.24, duration: 280})}/></div>
    {isLegalGraph && <div className="graph-status-legend" role="note"><span className="graph-status-legend-title">สถานะความสัมพันธ์</span><span><i className="verified" aria-hidden="true"/>เส้นทึบ · {reviewStatusLabel("verified")}</span><span><i className="suggested" aria-hidden="true"/>เส้นประ · {reviewStatusLabel("suggested")}</span><span><i className="manual" aria-hidden="true"/>สีม่วง · {relationshipOriginLabel("manual")}</span><span className="graph-status-legend-help">กดที่เส้นเพื่อดูเอกสารและข้อความหลักฐาน</span></div>}
    <div className="graph-layout"><div className={`graph-canvas ${isLegalGraph && entities.length > 10 ? "graph-canvas-dense" : ""}`}><ReactFlow nodes={nodes} edges={edges} nodeTypes={graphNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={onPaneClick} onNodeDragStop={onNodeDragStop} onEdgeClick={selectEdge} onConnect={onConnect} fitView fitViewOptions={{padding: 0.3}} minZoom={0.25} maxZoom={2} nodesConnectable connectionMode="loose" connectionRadius={24} defaultEdgeOptions={{type: "smoothstep"}}><Background gap={20} size={1} color="#b9cbd3"/><MiniMap pannable zoomable nodeColor="#2c7282"/><Controls showInteractive={false}/></ReactFlow></div>
      <aside className={`graph-inspector ${isInspectorOpen ? "open" : "closed"}`}>{isInspectorOpen && <button type="button" className="graph-inspector-close" onClick={closeInspector} aria-label="ปิดแผงรายละเอียด" style={{position: "absolute", top: 12, right: 14, border: 0, background: "transparent", color: "#52717a", fontSize: "1.5rem", lineHeight: 1, cursor: "pointer"}}>×</button>}{draftPosition ? <form className="form-stack" onSubmit={createNode}><p className="eyebrow">โหนดใหม่</p><h2>เพิ่มโหนดในกราฟ</h2><p className="section-copy">โหนดนี้จะถูกวางไว้ตรงตำแหน่งที่คุณคลิก</p><TextInput label="ชื่อโหนด" value={entityName} onChange={setEntityName} placeholder="เช่น Payment API" isRequired hasAutoFocus/><DesignSystemSelect label="ประเภท" value={entityType} onChange={setEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: type}))} size="md"/><Button label="เพิ่มโหนด" type="submit" variant="primary" isDisabled={!entityName.trim()}/></form> : selectedEntity ? selectedEntityPanel : selectedRelationship ? <form className="form-stack" onSubmit={updateSelectedRelationship}><p className="eyebrow">{selectedRelationship.review_status === "suggested" ? "ความสัมพันธ์ที่ระบบแนะนำ" : "ความสัมพันธ์ที่เลือก"}</p><div className="relationship-heading"><h2>{relationshipLabel(selectedRelationship.relationship_type)}</h2><Badge label={reviewStatusLabel(selectedRelationship.review_status)} variant={reviewBadgeVariant(selectedRelationship.review_status)}/></div><p className="section-copy"><b>แหล่งที่มา:</b> {relationshipOriginLabel(selectedRelationship.origin)}{selectedRelationship.confidence == null ? "" : ` · ความเชื่อมั่น ${Math.round(selectedRelationship.confidence * 100)}%`}</p><div className="relationship-direction" aria-label="ทิศทางความสัมพันธ์"><span className="relationship-entity"><b>{entityNamesById[selectedRelationship.source_entity_id] || "ไม่ทราบโหนดต้นทาง"}</b><code>{String(selectedRelationship.source_entity_id).slice(0, 8)}</code></span><span aria-hidden="true">→</span><span className="relationship-entity"><b>{entityNamesById[selectedRelationship.target_entity_id] || "ไม่ทราบโหนดปลายทาง"}</b><code>{String(selectedRelationship.target_entity_id).slice(0, 8)}</code></span></div>{selectedRelationship.sources?.length ? <div className="legal-evidence"><b>ข้อความหลักฐานจากเอกสาร</b>{selectedRelationship.sources.map(source => <details key={`${source.document_id}-${source.excerpt}`}><summary>{source.title}</summary><p>{source.excerpt || "ไม่พบข้อความหลักฐาน"}</p></details>)}</div> : <p className="section-copy">ยังไม่มีข้อความหลักฐานที่จัดเก็บสำหรับความสัมพันธ์นี้</p>}{selectedRelationship.origin === "ai_suggestion" && selectedRelationship.review_status === "suggested" ? <div className="preview-actions"><Button label="อนุมัติความสัมพันธ์" type="button" variant="primary" onClick={() => reviewLegalRelationship(selectedRelationship.id, "verified")}/><Button label="ปฏิเสธ" type="button" variant="destructive" onClick={() => reviewLegalRelationship(selectedRelationship.id, "rejected")}/></div> : (!isLegalGraph || legalGraphView === "manual") && <><DesignSystemSelect label="ประเภทความสัมพันธ์" value={editRelationshipType} onChange={setEditRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: relationshipLabel(type)}))} size="md"/><Button label="บันทึกความสัมพันธ์" type="submit" variant="primary"/><Button label="ลบความสัมพันธ์" type="button" variant="destructive" onClick={deleteSelectedRelationship}/></>}</form> : null}{graphNotice && <p className="graph-notice" role="status">{graphNotice}</p>}</aside></div>
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

## Query handling

- Forward the user's original question to the MCP tool unchanged. Do not
  tokenize, translate, summarize, or rewrite it before retrieval.
- For questions asking how many documents/laws exist, asking for all items, or
  asking how they are grouped by type, use \`document_inventory_summary\`.
- Pass the user's original question in the tool's \`query\` field.
- If that tool is not present in \`tools/list\` for this token, call
  \`search_knowledge\` with the original question unchanged; the server has a
  deterministic inventory fallback for this query shape.
- Do not infer a count from the number of citations or from a few examples in
  retrieved chunks. Use the registry summary's \`total_documents\` and \`groups\`.
- Treat \`scope: all\` as the complete non-deleted inventory. Use \`scope: current\`
  only when the user explicitly asks for current/in-force items.

## Tools (softnix-knowledge MCP server)

- \`search_knowledge\` — primary tool. Send the user's question; it returns a
  grounded answer, cited \`sources\` ([S1], [S2], …), and metadata. Use it for
  almost every question, before you write anything.
- \`document_inventory_summary\` — deterministic count and grouping from the
  document/legal registry. Use it for totals, complete lists, and type counts;
  its registry citations use [I#] and document rows use [D#].
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

1. If the question needs facts, call the most specific MCP tool FIRST — before writing
   any part of the answer.
2. Base the answer strictly on the returned \`answer\`, structured fields, and
   \`sources\`. Keep the [S#] or [I#] citations so the user can verify every claim.
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

function AccessView({selectedKb, knowledgeBases, tokens, auditLogs, loadAccess, createMcpToken, rotateMcpToken, changeTokenState}) {
  const allTools = ["search_knowledge", "document_inventory_summary", "find_entities", "analyze_relationships", "analyze_impact", "get_sources", "resolve_legal_context", "get_legal_instrument", "get_provision_history"];
  const activeKnowledgeBases = knowledgeBases.filter(kb => kb.status === "active");
  const kbNames = useMemo(() => Object.fromEntries(knowledgeBases.map(kb => [kb.id, kb.name])), [knowledgeBases]);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [accessLoading, setAccessLoading] = useState(true);
  const [accessLoadError, setAccessLoadError] = useState("");
  const [operations, setOperations] = useState(null);
  const [operationsError, setOperationsError] = useState("");
  const [formError, setFormError] = useState("");
  const [copyError, setCopyError] = useState("");
  const [copied, setCopied] = useState("");
  const [actionError, setActionError] = useState("");
  const [mutatingTokenId, setMutatingTokenId] = useState("");
  const [tokenFilter, setTokenFilter] = useState("all");
  const [tokenSearch, setTokenSearch] = useState("");
  const [selectedKbs, setSelectedKbs] = useState(selectedKb ? [selectedKb.id] : []);
  const [tools, setTools] = useState(allTools);
  const [expiresAt, setExpiresAt] = useState("");
  const [rpm, setRpm] = useState(60);
  const [concurrency, setConcurrency] = useState(5);
  const [timeout, setTimeoutValue] = useState(60);
  const secretTimer = useRef(null);
  const mcpUrl = `${window.location.origin}/mcp`;
  const tokenForGuide = secret || "YOUR_SOFTNIX_MCP_TOKEN";
  const cliCommand = `claude mcp add --transport http softnix-knowledge "${mcpUrl}" --header "Authorization: Bearer ${tokenForGuide}"`;
  const jsonConfig = JSON.stringify({mcpServers: {"softnix-knowledge": {type: "http", url: mcpUrl, headers: {Authorization: "Bearer ${SOFTNIX_MCP_TOKEN}"}}}}, null, 2);
  const skillContent = buildAgentSkillMd();
  const toggle = (value, current, setCurrent) => setCurrent(current.includes(value) ? current.filter(item => item !== value) : [...current, value]);
  const revealSecret = token => { if (secretTimer.current) window.clearTimeout(secretTimer.current); setSecret(token); secretTimer.current = window.setTimeout(() => setSecret(""), 120000); };
  const hideSecret = () => { if (secretTimer.current) window.clearTimeout(secretTimer.current); setSecret(""); };
  const copy = async (value, label) => { try { await navigator.clipboard.writeText(value); setCopyError(""); setCopied(label); window.setTimeout(() => setCopied(""), 1800); } catch { setCopyError("ไม่สามารถคัดลอกได้ กรุณาคัดลอกข้อความด้วยตนเอง"); } };
  const refreshAccess = async () => { setAccessLoading(true); setAccessLoadError(""); try { const result = await loadAccess(); if (result?.errors?.length) setAccessLoadError(result.errors.join(" · ")); } catch (error) { setAccessLoadError(error.message || "โหลดข้อมูล Access ไม่สำเร็จ"); } finally { setAccessLoading(false); } };
  const loadOperations = async () => { try { const [ready, projection] = await Promise.all([api("/v1/system/status"), api("/v1/system/graph-projection")]); setOperations({ready, projection}); setOperationsError(""); } catch (error) { setOperationsError(error.message || "โหลดสถานะระบบไม่สำเร็จ"); } };
  useEffect(() => { refreshAccess(); loadOperations(); return () => { if (secretTimer.current) window.clearTimeout(secretTimer.current); }; }, []);
  useEffect(() => { const activeIds = new Set(activeKnowledgeBases.map(kb => kb.id)); setSelectedKbs(current => { const retained = current.filter(id => activeIds.has(id)); if (retained.length || !selectedKb || !activeIds.has(selectedKb.id)) return retained; return [selectedKb.id]; }); }, [selectedKb, knowledgeBases]);
  const create = async event => { event.preventDefault(); setIsLoading(true); setFormError(""); try { const result = await createMcpToken({name, allowed_knowledge_base_ids: selectedKbs, allowed_tools: tools, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null, requests_per_minute: Number(rpm), max_concurrent_requests: Number(concurrency), query_timeout_seconds: Number(timeout)}); revealSecret(result.token); setName(""); } catch (error) { setFormError(error.message || "สร้าง MCP token ไม่สำเร็จ"); } finally { setIsLoading(false); } };
  const rotate = async token => { if (!window.confirm(`Rotate key for ${token.name}? The current key will be revoked immediately.`)) return; setMutatingTokenId(token.id); setActionError(""); try { const result = await rotateMcpToken(token.id); revealSecret(result.token); await copy(result.token, "token"); } catch (error) { setActionError(error.message || "หมุน token ไม่สำเร็จ"); } finally { setMutatingTokenId(""); } };
  const changeState = async (token, action) => { if (action === "revoke" && !window.confirm(`Revoke ${token.name}? This cannot be undone and connected agents will stop working.`)) return; if (action === "disable" && !window.confirm(`Disable ${token.name}? Connected agents will stop working until it is enabled again.`)) return; setMutatingTokenId(`${token.id}:${action}`); setActionError(""); try { await changeTokenState(token.id, action); } catch (error) { setActionError(error.message || "เปลี่ยนสถานะ token ไม่สำเร็จ"); } finally { setMutatingTokenId(""); } };
  const visibleTokens = tokens.filter(token => { const matchStatus = tokenFilter === "all" || token.status === tokenFilter; const needle = tokenSearch.trim().toLocaleLowerCase(); const matchSearch = !needle || `${token.name} ${token.token_prefix}`.toLocaleLowerCase().includes(needle); return matchStatus && matchSearch; });
  const statusLabel = {active: "ใช้งาน", inactive: "ปิดใช้งาน", revoked: "เพิกถอนแล้ว"};
  return <><PageHeading eyebrow="ACCESS & MCP" title="Connect knowledge safely" description="Create a scoped token, copy a ready-to-run configuration, then verify the connection." actions={<Button label="Refresh status" variant="ghost" isLoading={accessLoading} onClick={() => { refreshAccess(); loadOperations(); }}/>}/>{(accessLoadError || operationsError || copyError) && <p className="inline-error access-error" role="alert">{accessLoadError || operationsError || copyError}</p>}<section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operationsError ? "Unavailable" : operations?.ready?.status || "Checking system"}</b><span>{operations ? `${Object.keys(operations.ready.dependencies || {}).length} dependencies online` : operationsError || "Loading dependencies"}</span></div></div><div className="mcp-endpoint"><span>Server endpoint</span><code>{mcpUrl}</code><button type="button" onClick={() => copy(mcpUrl, "endpoint")}>Copy</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">STEP 1</p><h2>Create a scoped token</h2></div><Badge label="Secret shown once" variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label="Token name" value={name} onChange={setName} placeholder="e.g. claude-code-architecture" isRequired/><div className="scope-section"><div className="scope-heading"><b>Knowledge Base access</b>{activeKnowledgeBases.length > 0 && <button type="button" onClick={() => setSelectedKbs(activeKnowledgeBases.map(kb => kb.id))}>Select all</button>}</div><p className="section-copy">Only active Knowledge Bases can be granted to an MCP token.</p><div className="scope-options">{activeKnowledgeBases.length ? activeKnowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${selectedKbs.includes(kb.id) ? "selected" : ""}`}><input type="checkbox" checked={selectedKbs.includes(kb.id)} onChange={() => toggle(kb.id, selectedKbs, setSelectedKbs)}/><span>{kb.name}</span></label>) : <p className="section-copy">No active Knowledge Bases. Activate one from Knowledge Bases before creating a token.</p>}</div></div><div className="scope-section"><div className="scope-heading"><b>Allowed tools</b><button type="button" onClick={() => setTools(allTools)}>Enable all</button></div><div className="tool-options">{allTools.map(tool => <label key={tool} className={`tool-option ${tools.includes(tool) ? "selected" : ""}`}><input type="checkbox" checked={tools.includes(tool)} onChange={() => toggle(tool, tools, setTools)}/><span>{tool.replace(/_/g, " ")}</span></label>)}</div></div><details className="advanced-options"><summary>Advanced limits</summary><div className="limit-grid"><label>Expiry (optional)<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>Requests/min<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>Concurrent requests<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>Timeout (seconds)<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error" role="alert">{formError}</p>}<Button label="Create MCP token" type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !tools.length || !selectedKbs.length}/></form></Card><Card padding={4}><p className="eyebrow">STEP 2</p><h2>Connect with Claude Code</h2><p className="section-copy">Run this command on the machine where Claude Code is installed. Use a HTTPS URL for access outside this computer.</p><div className="code-panel"><div className="code-panel-top"><b>Terminal</b><button type="button" onClick={() => copy(cliCommand, "claude command")}>{copied === "claude command" ? "Copied" : "Copy command"}</button></div><pre>{cliCommand}</pre></div><ol className="mcp-steps"><li>Create the token in Step 1 and copy it immediately.</li><li>Paste the command into Terminal.</li><li>Restart Claude Code, then run <code>/mcp</code> to confirm <code>softnix-knowledge</code> is connected.</li></ol><details className="json-config"><summary>Prefer a project <code>.mcp.json</code> file?</summary><p>Store the token in <code>SOFTNIX_MCP_TOKEN</code>, not in source control.</p><div className="code-panel"><div className="code-panel-top"><b>.mcp.json</b><button type="button" onClick={() => copy(jsonConfig, "json config")}>{copied === "json config" ? "Copied" : "Copy JSON"}</button></div><pre>{jsonConfig}</pre></div></details><details className="json-config skill-config"><summary>Add an agent Skill <Badge label="recommended" variant="success"/></summary><p>Make any connected agent answer <b>only</b> from the Knowledge Bases authorized by this MCP token and never from web search, browsing, or its own training data — so users never get answers silently mixed from other sources.</p><p className="section-copy">Follows the open <a href="https://agentskills.io" target="_blank" rel="noreferrer">agentskills.io</a> Agent Skill standard, so it works with Claude Code and any other compatible agent tool (Cursor, Gemini CLI, VS Code, GitHub Copilot, and more). Save this as <code>SKILL.md</code> inside a folder named exactly <code>softnix-knowledge</code> in your agent's skills directory — for Claude Code that is <code>.claude/skills/softnix-knowledge/SKILL.md</code>.</p><div className="code-panel"><div className="code-panel-top"><b>SKILL.md</b><button type="button" onClick={() => copy(skillContent, "skill")}>{copied === "skill" ? "Copied" : "Copy SKILL"}</button></div><pre className="skill-preview">{skillContent}</pre></div></details>{secret && <div className="token-reveal"><b>New token — copy it now</b><code>{secret}</code><div className="token-reveal-actions"><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? "Copied" : "Copy token"}</button><button type="button" onClick={hideSecret}>Hide token</button></div></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">TOKEN MANAGEMENT</p><h2>MCP tokens</h2></div><span className="section-copy">Revoke a token immediately if a machine or credential is no longer trusted.</span></div><div className="token-filter-bar"><TextInput label="Find a token" value={tokenSearch} onChange={setTokenSearch} placeholder="Token name or prefix"/><DesignSystemSelect label="Status" value={tokenFilter} onChange={setTokenFilter} options={[{value: "all", label: "All statuses"}, ...Object.entries(statusLabel).map(([value, label]) => ({value, label}))]}/></div>{actionError && <p className="inline-error" role="alert">{actionError}</p>}{accessLoading && !tokens.length ? <p className="section-copy" role="status">Loading tokens…</p> : visibleTokens.length ? <div className="token-list">{visibleTokens.map(token => { const busy = mutatingTokenId.startsWith(`${token.id}:`) || mutatingTokenId === token.id; return <article className="token-row" key={token.id}><div><b>{token.name}</b><p>{token.token_prefix}… · {token.allowed_tools.length} tools · {token.allowed_knowledge_base_ids.length} Knowledge Base(s)</p><small>{token.requests_per_minute}/min · {token.max_concurrent_requests} concurrent · {token.query_timeout_seconds}s timeout{token.expires_at ? ` · expires ${new Date(token.expires_at).toLocaleString()}` : ""}</small><details className="token-scope-details"><summary>View access scope</summary><div><b>Knowledge Bases</b><p>{token.allowed_knowledge_base_ids.map(id => kbNames[id] || id).join(", ") || "None"}</p><b>Tools</b><p>{token.allowed_tools.map(tool => tool.replace(/_/g, " ")).join(", ") || "None"}</p></div></details></div><StatusBadge status={token.status}/><div className="document-actions">{token.status !== "revoked" && <Button label="Rotate key" size="sm" variant="secondary" isLoading={busy && mutatingTokenId === token.id} isDisabled={Boolean(mutatingTokenId)} onClick={() => rotate(token)}/>} {token.status === "active" && <Button label="Disable" size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":disable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "disable")}/>} {token.status === "inactive" && <Button label="Enable" size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":enable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "enable")}/>} {token.status !== "revoked" && <Button label="Revoke" size="sm" variant="destructive" isLoading={busy && mutatingTokenId.endsWith(":revoke")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "revoke")}/>}</div></article>; })}</div> : <EmptyState title={tokens.length ? "No matching tokens" : "No MCP tokens yet"} description={tokens.length ? "Try another status or search term." : "Create a token above to connect Claude Code or another MCP client."}/>}</section><section className="content-section"><h2>Recent audit activity</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || "system"} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title="No activity yet" description="Administrative actions will appear here."/>}</section></>;
}

function LegacyAccessView({selectedKb, knowledgeBases, tokens, auditLogs, loadAccess, createMcpToken, rotateMcpToken, changeTokenState}) {
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


const Impact = ({data}) => <div className="result-panel"><h3>{data.insufficient_evidence ? "Insufficient evidence" : `Impact for ${data.subject.name}`}</h3>{data.insufficient_evidence ? <p>Upload more source material or add verified relationships before making a decision.</p> : <><h4>Direct impact</h4><ul>{data.direct_impacts.map(item => <li key={item.entity_id}>{item.name} <Badge label={item.relationship} variant="warning"/> {item.citation_ids.join(" ")}</li>)}</ul><h4>Indirect impact</h4><ul>{data.indirect_impacts.map(item => <li key={item.entity_id}>{item.path.join(" → ")} {item.citation_ids.join(" ")}</li>)}</ul></>}</div>;
const Graph = ({data}) => <div className="result-panel"><div className="graph-summary"><Badge label={`${data.nodes.length} nodes`} variant="info"/><Badge label={`${data.edges.length} connections`} variant="neutral"/></div><ul className="graph-list">{data.edges.map(edge => <li key={edge.id}><b>{data.nodes.find(node => node.id === edge.source)?.name}</b><span>{edge.type.replace(/_/g, " ")}</span><b>{data.nodes.find(node => node.id === edge.target)?.name}</b></li>)}</ul></div>;
const LEGAL_STATUS_VARIANTS = {in_force: "success", amended: "warning", not_yet_effective: "neutral", unknown: "neutral", superseded: "error", repealed: "error"};
const LegalStatusBadge = ({status}) => status ? <Badge label={legalStatusLabel(status)} variant={LEGAL_STATUS_VARIANTS[status] || "neutral"}/> : null;

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

function DocumentPreview({preview, jobs, templates, legalInstrument, onExtractLegal, onSaveLegal, onDeleteLegal, onSaveDocumentMetadata, onUpdateLegalInstrument, onClose}) {
  const [editingLegal, setEditingLegal] = useState(false);
  const [legalDraft, setLegalDraft] = useState("");
  const [legalError, setLegalError] = useState("");
  const [editingMetadata, setEditingMetadata] = useState(false);
  const [metadataDraft, setMetadataDraft] = useState({});
  const hasLegalMetadata = Boolean(preview.legal_metadata && Object.keys(preview.legal_metadata).length);
  useEffect(() => { setEditingLegal(false); setLegalError(""); setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); }, [preview.document_id, preview.legal_metadata]);
  useEffect(() => { setEditingMetadata(false); setMetadataDraft(preview.document_metadata || {}); }, [preview.document_id, preview.document_metadata]);
  const startEditing = () => { setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); setLegalError(""); setEditingLegal(true); };
  const save = async event => {
    event.preventDefault();
    try {
      const parsed = JSON.parse(legalDraft);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("Metadata must be a JSON object.");
      await onSaveLegal({id: preview.document_id, title: preview.title}, parsed); setEditingLegal(false); setLegalError("");
    } catch (error) { setLegalError(error instanceof SyntaxError ? "Use valid JSON before saving." : error.message); }
  };
  const template = templates.find(row => row.id === preview.metadata_template_id);
  const documentFields = preview.metadata_template_fields?.length ? preview.metadata_template_fields : (template?.fields || []);
  const hasDocumentFields = Boolean(documentFields.length);
  const saveMetadata = async event => { event.preventDefault(); await onSaveDocumentMetadata({id: preview.document_id, title: preview.title}, metadataDraft); setEditingMetadata(false); };
  return <section className="preview-section"><Card padding={4}><div className="preview-heading"><div><p className="eyebrow">DOCUMENT DETAILS</p><h2>{preview.title}</h2></div><div className="preview-actions"><Button label="Back to document library" size="sm" variant="ghost" onClick={onClose}/><StatusBadge status={preview.status}/>{preview.status === "completed" && <Button label="Extract legal metadata" size="sm" variant="secondary" onClick={() => onExtractLegal({id: preview.document_id, title: preview.title})}/>}</div></div>
    {legalInstrument && <LegalInstrumentCard instrument={legalInstrument} onUpdate={onUpdateLegalInstrument}/>}
    {preview.error_code && <p className="inline-error">{preview.error_code}</p>}<pre className="excerpt">{preview.text || "Text will appear here when processing is complete."}</pre>{hasDocumentFields && <div className="document-metadata-panel"><div className="preview-heading"><div><h3>{preview.metadata_template_name || "Document metadata"}</h3><p className="section-copy">Fields supplied by the uploader. Edit only when the source record changes.</p></div>{!editingMetadata && <Button label="Edit fields" size="sm" variant="secondary" onClick={() => setEditingMetadata(true)}/>}</div>{editingMetadata ? <form onSubmit={saveMetadata}><MetadataFields fields={documentFields} values={metadataDraft} onChange={setMetadataDraft}/><div className="preview-actions"><Button label="Save fields" type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={() => { setMetadataDraft(preview.document_metadata || {}); setEditingMetadata(false); }}/></div></form> : <dl className="document-metadata-values">{documentFields.filter(field => preview.document_metadata?.[field.key] !== undefined && preview.document_metadata?.[field.key] !== "").map(field => <div key={field.key}><dt>{field.label}</dt><dd>{String(preview.document_metadata[field.key])}</dd></div>)}</dl>}</div>}<div className="legal-metadata-panel"><div className="preview-heading"><div><h3>Legal metadata</h3><p className="section-copy">Legal Graph Schema v2 keeps the instrument, provisions, and cross-document references with evidence. Suggested links still require review.</p></div>{!editingLegal && <div className="preview-actions"><Button label={hasLegalMetadata ? "Edit metadata" : "Add metadata"} size="sm" variant="secondary" onClick={startEditing}/>{hasLegalMetadata && <Button label="Delete metadata" size="sm" variant="destructive" onClick={() => onDeleteLegal({id: preview.document_id, title: preview.title})}/>}</div>}</div>{editingLegal ? <form className="legal-editor" onSubmit={save}><textarea aria-label="Legal metadata JSON" value={legalDraft} onChange={event => setLegalDraft(event.target.value)} rows={18} spellCheck="false"/><p className="section-copy">Use <code>instrument</code>, <code>provisions</code>, and <code>references</code>; every fact needs an <code>evidence_quote</code>. Saving queues a safe graph rebuild.</p>{legalError && <p className="inline-error" role="alert">{legalError}</p>}<div className="preview-actions"><Button label="Save metadata" type="submit" variant="primary"/><Button label="Cancel" type="button" variant="ghost" onClick={() => setEditingLegal(false)}/></div></form> : hasLegalMetadata ? <pre className="excerpt legal-metadata">{JSON.stringify(preview.legal_metadata, null, 2)}</pre> : <p className="section-copy">ยังไม่มี legal metadata — กด Add metadata เพื่อเพิ่มเอง หรือ Extract legal metadata เพื่อสกัดจากเอกสาร</p>}</div><h3>Processing activity</h3>{jobs.length ? <div className="job-list">{jobs.map(job => <div key={job.id}><span>{job.type || "PROCESS_DOCUMENT"} · {job.stage || "queued"}{job.attempt_count ? ` · attempt ${job.attempt_count}` : ""}{job.error_code ? ` · ${job.error_code}` : ""}{job.error_message ? `: ${job.error_message}` : ""}</span><StatusBadge status={job.status}/><span>{job.progress_percent}%</span></div>)}</div> : <p className="section-copy">No processing jobs have been recorded yet.</p>}</Card></section>;
}

createRoot(document.getElementById("root")).render(<App/>);
