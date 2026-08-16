import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {createRoot} from "react-dom/client";
import {Theme, AppShell, Badge, Button, Card, CheckboxInput, CommandPalette, EmptyState, FileInput, ProgressBar, SideNav, SideNavHeading, SideNavItem, SideNavSection, Selector, TextArea, TextInput, Toast, TopNav, TopNavHeading, useDialogFocus} from "./ui.jsx";
import {AppWindow, BookOpen, Buildings, ChartLineUp, CirclesThree, Cloud, Compass, Database, FileText, Gavel, GitBranch, HardDrives, Key, Lightbulb, MagnifyingGlass, Scales, ShieldCheck, User, Users, UsersThree} from "@phosphor-icons/react";
import {Background, Controls, Handle, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, useEdgesState, useNodesState, useReactFlow} from "@xyflow/react";
import "./kumo.css";
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
import "./cloudflare-overrides.css";
import {connectionHandles} from "./graph-geometry.mjs";
import {LanguageProvider, useLanguage} from "./language.jsx";
import {legalLabels} from "./translations.js";

const ACCEPTED_FILES = ".pdf,.docx,.pptx,.xlsx,.xls,.txt,.md,.html,.htm,.csv,.json";
const MAX_FILE_SIZE_MB = Math.max(1, Number(import.meta.env.VITE_MAX_FILE_SIZE_MB || 100));
const MAX_FILE_SIZE = MAX_FILE_SIZE_MB * 1024 * 1024;
const WORKSPACE_VIEWS = new Set(["knowledge-bases", "documents", "search", "explore", "mcp-tokens", "ingest-tokens", "logs", "users", "groups", "profile"]);
const ROLE_LEVEL = {user: 0, manager: 1, admin: 2};
const isActiveProcessingJob = job => ["queued", "running"].includes(job?.status);
const DOCUMENT_TYPE_OPTIONS = [
  {value: "general", labelKey: "documentType.general.label", descriptionKey: "documentType.general.description"},
  {value: "legal", labelKey: "documentType.legal.label", descriptionKey: "documentType.legal.description"},
  {value: "regulation", labelKey: "documentType.regulation.label", descriptionKey: "documentType.regulation.description"},
  {value: "contract", labelKey: "documentType.contract.label", descriptionKey: "documentType.contract.description"},
];
const documentTypeLabel = (t, type) => t(DOCUMENT_TYPE_OPTIONS.find(option => option.value === type)?.labelKey || "documentType.general.label");
const documentTypeDescription = (t, type) => t(DOCUMENT_TYPE_OPTIONS.find(option => option.value === type)?.descriptionKey || "documentType.general.description");

// Workspace location is part of the browser address rather than transient UI
// state, so a reload (or a copied URL) keeps the user in the same context.
const readWorkspaceRoute = () => {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view");
  return {view: WORKSPACE_VIEWS.has(view) ? view : "knowledge-bases", knowledgeBaseId: params.get("kb") || ""};
};

// Keep native HTML semantics where the browser owns the interaction (file/date
// inputs), while routing choice fields through the shared Kumo-style adapter.
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
  const {language, setLanguage, t} = useLanguage();
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
  return <main className="login-page"><section className="login-card">
    <Button className="login-language-toggle" label={language === "th" ? "EN" : "TH"} size="sm" variant="ghost" onClick={() => setLanguage(language === "th" ? "en" : "th")} aria-label={t("app.toggleLanguage")}/>
    <img className="login-logo" src="/logo-softnix.png" alt="Softnix"/>
    <p className="eyebrow">{t("login.brand")}</p><h1>{t("login.tagline")}</h1>
    <p className="login-copy">{t("login.description")}</p>
    <form className="form-stack" onSubmit={submit}>
      <TextInput label={t("login.username")} value={username} onChange={setUsername} isRequired hasAutoFocus/>
      <TextInput label={t("login.password")} type="password" value={password} onChange={setPassword} isRequired/>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <Button label={t("login.submit")} type="submit" variant="primary" size="lg" isLoading={isLoading}/>
    </form>
  </section></main>;
}

function App() {
  const {language, setLanguage, t} = useLanguage();
  const initialRoute = useMemo(() => readWorkspaceRoute(), []);
  const [user, setUser] = useState(null);
  const [isSessionLoading, setIsSessionLoading] = useState(true);
  const [kbs, setKbs] = useState([]);
  const [selectedKbId, setSelectedKbId] = useState(() => initialRoute.knowledgeBaseId);
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
  const [documentJobPolling, setDocumentJobPolling] = useState(false);
  const [documentJobPollError, setDocumentJobPollError] = useState("");
  const [graph, setGraph] = useState(null);
  const [impact, setImpact] = useState(null);
  const [query, setQuery] = useState("");
  const [queryAsOfDate, setQueryAsOfDate] = useState("");
  const [queryIncludeHistorical, setQueryIncludeHistorical] = useState(false);
  const [queryResult, setQueryResult] = useState(null);
  const [isQuerying, setIsQuerying] = useState(false);
  const [message, setMessage] = useState(null);
  const [activeView, setActiveView] = useState(() => initialRoute.view);
  const [isCommandPaletteOpen, setIsCommandPaletteOpen] = useState(false);
  const [viewTrail, setViewTrail] = useState(() => [initialRoute.view]);
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
  const [users, setUsers] = useState([]);
  const [groups, setGroups] = useState([]);
  const selectedKb = useMemo(() => kbs.find(kb => kb.id === selectedKbId), [kbs, selectedKbId]);
  const loadRequestRef = useRef(0);

  const notify = (body, type = "info") => setMessage({body, type, id: Date.now()});
  const userRole = user?.role || "user";
  const userRoleLevel = ROLE_LEVEL[userRole] ?? 0;
  const loadUsers = useCallback(async () => setUsers(await api("/v1/users")), []);
  const loadGroups = useCallback(async () => setGroups(await api("/v1/groups")), []);

  const showError = error => notify(error.message || t("app.error.generic"), "error");
  const closeCommandPalette = useCallback(() => setIsCommandPaletteOpen(false), []);
  const writeWorkspaceRoute = useCallback((view, knowledgeBaseId, {replace = false} = {}) => {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    if (knowledgeBaseId) url.searchParams.set("kb", knowledgeBaseId);
    else url.searchParams.delete("kb");
    const nextLocation = `${url.pathname}${url.search}${url.hash}`;
    const currentLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (nextLocation !== currentLocation) window.history[replace ? "replaceState" : "pushState"](window.history.state, "", nextLocation);
  }, []);
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
  useEffect(() => {
    if (!user) return;
    // Selection can also change from cards or API responses, outside a view
    // navigation. Keep the current history entry accurate in those cases.
    writeWorkspaceRoute(activeView, selectedKbId, {replace: true});
  }, [user, activeView, selectedKbId, writeWorkspaceRoute]);
  useEffect(() => {
    const restoreWorkspaceRoute = () => {
      const route = readWorkspaceRoute();
      setActiveView(route.view);
      setSelectedKbId(route.knowledgeBaseId);
      setViewTrail([route.view]);
      setDocumentPreview(null);
    };
    window.addEventListener("popstate", restoreWorkspaceRoute);
    return () => window.removeEventListener("popstate", restoreWorkspaceRoute);
  }, []);
  const loadKbs = async () => {
    const response = await api("/v1/knowledge-bases");
    const rows = Array.isArray(response) ? response : response.items || [];
    setKbs(rows);
    setSelectedKbId(current => rows.some(kb => kb.id === current) ? current : rows[0]?.id || "");
  };
  const loadKbData = async (id, includeDeleted = showDeletedDocuments, options = {}) => {
    const {background = false} = options;
    const requestId = ++loadRequestRef.current;
    const isCurrentRequest = () => requestId === loadRequestRef.current;
    if (!id) { if (isCurrentRequest()) { setEntities([]); setRelationships([]); setDocuments([]); setDocumentTemplates([]); setDocumentTotal(0); setProcessingDocumentsTotal(0); setHasCompletedDocuments(false); setIsLegalGraph(false); setLegalInstruments([]); setDocumentsLoading(false); } return; }
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
      if (!isCurrentRequest()) return;
      const nextDocuments = documentPage.items || [];
      const hasLegalDocuments = Boolean(documentPage.has_legal_documents);
      const nextLegalInstruments = hasLegalDocuments ? await api(`/v1/knowledge-bases/${id}/legal-registry`) : [];
      if (!isCurrentRequest()) return;
      setDocuments(nextDocuments); setDocumentTotal(documentPage.total || 0);
      setDocumentTemplates(templates || []);
      setProcessingDocumentsTotal(documentPage.processing_count || 0);
      setHasCompletedDocuments(Boolean(documentPage.has_completed_documents));
      setLegalInstruments(nextLegalInstruments);
      setIsLegalGraph(hasLegalDocuments);
      if (activeView === "explore" || (activeView === "documents" && hasLegalDocuments)) {
        const graphData = hasLegalDocuments
          ? await api(`/v1/knowledge-bases/${id}/legal-graph?view=${legalGraphView}`)
          : await Promise.all([api(`/v1/knowledge-bases/${id}/entities`), api(`/v1/knowledge-bases/${id}/relationships`)]);
        if (!isCurrentRequest()) return;
        const [nextEntities, nextRelationships] = hasLegalDocuments ? [graphData.nodes, graphData.edges] : graphData;
        setEntities(nextEntities); setRelationships(nextRelationships);
        if (!background) { setGraph(null); setImpact(null); }
      }
    } catch (error) {
      if (isCurrentRequest()) throw error;
    } finally { if (isCurrentRequest()) setDocumentsLoading(false); }
  };
  useEffect(() => { if (user) loadKbs().catch(showError); }, [user]);
  useEffect(() => { setLegalRebuildStatus(null); }, [selectedKbId]);
  useEffect(() => { if (selectedKbId) { setDocumentOffset(0); setDocumentPreview(null); setDocumentJobs([]); setDocumentTypeFilter("all"); } }, [selectedKbId]);
  useEffect(() => { if (user) loadKbData(selectedKbId).catch(showError); }, [selectedKbId, user, showDeletedDocuments, legalGraphView, activeView, documentOffset, documentSearch, documentStatusFilter, documentTypeFilter]);
  useEffect(() => {
    if (!user || activeView !== "documents" || !selectedKbId || processingDocumentsTotal === 0) return undefined;
    const timer = window.setInterval(() => loadKbData(selectedKbId, undefined, {background: true}).catch(showError), 5000);
    return () => window.clearInterval(timer);
  }, [activeView, selectedKbId, user, processingDocumentsTotal, showDeletedDocuments]);
  const hasActiveDocumentJobs = documentJobs.some(isActiveProcessingJob);
  useEffect(() => {
    const documentId = documentPreview?.document_id;
    if (!user || !documentId || !hasActiveDocumentJobs) {
      setDocumentJobPolling(false);
      return undefined;
    }
    let cancelled = false;
    let timer;
    setDocumentJobPolling(true);
    setDocumentJobPollError("");
    const poll = async () => {
      try {
        const [nextPreview, nextJobs] = await Promise.all([
          api(`/v1/documents/${documentId}/text`),
          api(`/v1/documents/${documentId}/jobs`),
        ]);
        if (cancelled) return;
        setDocumentJobs(nextJobs);
        setDocumentPreview(current => current && current.document_id === documentId
          ? {...current, ...nextPreview, title: current.title}
          : current);
        setDocumentJobPollError("");
        if (nextJobs.some(isActiveProcessingJob)) {
          timer = window.setTimeout(poll, 2000);
        } else {
          setDocumentJobPolling(false);
        }
      } catch (error) {
        if (cancelled) return;
        setDocumentJobPollError(error.message || t("app.error.generic"));
        timer = window.setTimeout(poll, 5000);
      }
    };
    timer = window.setTimeout(poll, 0);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [user, documentPreview?.document_id, hasActiveDocumentJobs]);
  useEffect(() => {
    if (!user || !selectedKbId || !legalRebuildStatus || !["queued", "running"].includes(legalRebuildStatus.status)) return undefined;
    const poll = async () => {
      try {
        const status = await api(`/v1/knowledge-bases/${selectedKbId}/legal-graph/rebuild`);
        setLegalRebuildStatus(status);
        if (status.status === "completed") { await loadKbData(selectedKbId); notify(t("app.notify.legalGraphRebuildCompleted")); }
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
      setKbs(items => [...items, kb]); setSelectedKbId(kb.id); setNewKbName(""); switchView("documents"); notify(t("app.notify.kbCreated"));
    } catch (error) { showError(error); }
  };
  const manageKnowledgeBase = async (knowledgeBase, action) => {
    if (action === "delete" && !window.confirm(t("app.confirm.deleteKb", {name: knowledgeBase.name}))) return;
    try {
      await api(`/v1/knowledge-bases/${knowledgeBase.id}${action === "delete" ? "" : `/${action}`}`, {method: action === "delete" ? "DELETE" : "POST"});
      await loadKbs();
      notify(t(action === "delete" ? "app.notify.kbDeleted" : action === "disable" ? "app.notify.kbDisabled" : "app.notify.kbActivated"));
    } catch (error) { showError(error); }
  };
  const updateRetrievalConfig = async (knowledgeBase, config) => {
    try {
      const updated = await api(`/v1/knowledge-bases/${knowledgeBase.id}/retrieval-config`, {method: "PATCH", body: JSON.stringify(config)});
      setKbs(items => items.map(item => item.id === updated.id ? updated : item));
      notify(t("app.notify.retrievalPolicyUpdated"));
    } catch (error) { showError(error); }
  };
  const updateKnowledgeBaseIcon = async (knowledgeBase, icon) => {
    try {
      const updated = await api(`/v1/knowledge-bases/${knowledgeBase.id}/icon`, {method: "PATCH", body: JSON.stringify({icon})});
      setKbs(items => items.map(item => item.id === updated.id ? updated : item));
      notify(t("app.notify.kbIconUpdated"));
      return true;
    } catch (error) {
      showError(error);
      return false;
    }
  };
  const addEntity = async ({name, entityType}) => {
    if (!selectedKbId || !name?.trim()) return null;
    try {
      const entity = await api(`/v1/knowledge-bases/${selectedKbId}/entities`, {method: "POST", body: JSON.stringify({name: name.trim(), entity_type: entityType || "Application"})});
      setEntities(items => [...items, entity]); notify(t("app.notify.entityAdded")); return entity;
    } catch (error) { showError(error); }
  };
  const addRelationship = async ({sourceEntityId, targetEntityId, relationshipType}) => {
    if (!selectedKbId || !sourceEntityId || !targetEntityId || sourceEntityId === targetEntityId) return null;
    try {
      const relationship = await api(`/v1/knowledge-bases/${selectedKbId}/relationships`, {method: "POST", body: JSON.stringify({source_entity_id: sourceEntityId, target_entity_id: targetEntityId, relationship_type: relationshipType || "DEPENDS_ON"})});
      setRelationships(items => [...items, relationship]); notify(t("app.notify.relationshipAdded")); return relationship;
    } catch (error) { showError(error); }
  };
  const syncGraphFromDocuments = async () => {
    if (!selectedKbId) return;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/graph/sync`, {method: "POST"});
      await loadKbData(selectedKbId);
      notify(t("app.notify.graphSyncCompleted", {entities: result.entities, relationships: result.relationships}));
      return result;
    } catch (error) { showError(error); return null; }
  };
  const queueLegalGraphRebuild = async () => {
    if (!selectedKbId) return null;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/legal-graph/rebuild`, {method: "POST"});
      setLegalRebuildStatus({status: result.status, progress_percent: 0});
      notify(t("app.notify.legalGraphRebuildQueued"));
      return result;
    } catch (error) { showError(error); return null; }
  };
  const reviewLegalRelationship = async (relationshipId, status) => {
    try {
      await api(`/v1/relationships/${relationshipId}/legal-review`, {method: "PATCH", body: JSON.stringify({status})});
      await loadKbData(selectedKbId); notify(status === "verified" ? t("app.notify.legalRelationshipApproved") : t("app.notify.legalRelationshipRejected"));
    } catch (error) { showError(error); }
  };
  const resolveLegalRegistry = async () => {
    if (!selectedKbId) return;
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/legal-registry/resolve`, {method: "POST"});
      await loadKbData(selectedKbId); notify(t("app.notify.legalRegistryResolved", {changed: result.changed, instruments: result.instruments}));
    } catch (error) { showError(error); }
  };
  const updateLegalInstrument = async (instrumentId, payload) => {
    try { await api(`/v1/legal-instruments/${instrumentId}`, {method: "PATCH", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify(t("app.notify.legalInstrumentUpdated")); }
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
    event.preventDefault(); if (!selectedKbId || !uploadFile.length) return false;
    const template = documentTemplates.find(row => row.id === uploadTemplateId);
    const form = new FormData(); uploadFile.forEach(file => form.append("files", file)); form.append("document_type", template?.base_document_type || uploadDocumentType); form.append("template_id", uploadTemplateId); form.append("metadata_json", JSON.stringify(uploadMetadata)); if (uploadFile.length === 1 && uploadTitle.trim()) form.append("title", uploadTitle.trim());
    setIsUploading(true);
    try {
      const result = await api(`/v1/knowledge-bases/${selectedKbId}/documents/batch`, {method: "POST", body: form});
      const selectedCount = uploadFile.length;
      setUploadFile([]); setUploadTitle(""); setUploadDocumentType("general"); setUploadTemplateId("system:general"); setUploadMetadata({}); await loadKbData(selectedKbId);
      notify(result.failed_count ? t("app.notify.uploadPartialFailure", {queued: result.queued_count, total: selectedCount, failed: result.failed_count}) : t("app.notify.uploadQueued", {queued: result.queued_count}));
      return true;
    } catch (error) { showError(error); return false; }
    finally { setIsUploading(false); }
  };
  const extractLegalMetadata = async document => {
    try {
      setDocumentJobPollError("");
      await api(`/v1/documents/${document.id}/legal-extract`, {method: "POST"});
      await openDocument(document);
      notify(t("app.notify.legalExtractQueued"));
    }
    catch (error) { showError(error); }
  };
  const saveLegalMetadata = async (document, metadata) => {
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "PUT", body: JSON.stringify({metadata})}); await openDocument(document); await queueLegalGraphRebuild(); notify(t("app.notify.legalMetadataSaved")); }
    catch (error) { showError(error); throw error; }
  };
  const deleteLegalMetadata = async document => {
    if (!window.confirm(t("app.confirm.deleteLegalMetadata"))) return;
    try { await api(`/v1/documents/${document.id}/legal-metadata`, {method: "DELETE"}); await openDocument(document); await queueLegalGraphRebuild(); notify(t("app.notify.legalMetadataDeleted")); }
    catch (error) { showError(error); }
  };
  const saveDocumentMetadata = async (document, values) => {
    try { await api(`/v1/documents/${document.id}/metadata`, {method: "PATCH", body: JSON.stringify({values})}); await openDocument(document); await loadKbData(selectedKbId); notify(t("app.notify.documentMetadataSaved")); }
    catch (error) { showError(error); throw error; }
  };
  const createDocumentTemplate = async payload => {
    try { await api(`/v1/knowledge-bases/${selectedKbId}/document-templates`, {method: "POST", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify(t("app.notify.documentTypeCreated")); }
    catch (error) { showError(error); throw error; }
  };
  const updateDocumentTemplate = async (templateId, payload) => {
    try { await api(`/v1/document-templates/${templateId}`, {method: "PATCH", body: JSON.stringify(payload)}); await loadKbData(selectedKbId); notify(t("app.notify.documentTypeUpdated")); }
    catch (error) { showError(error); throw error; }
  };
  const deactivateDocumentTemplate = async template => {
    if (!window.confirm(t("app.confirm.disableDocumentType", {name: template.name}))) return;
    try { await api(`/v1/document-templates/${template.id}`, {method: "DELETE"}); if (uploadTemplateId === template.id) { setUploadTemplateId("system:general"); setUploadDocumentType("general"); setUploadMetadata({}); } if (documentTypeFilter === template.id) { setDocumentTypeFilter("all"); setDocumentOffset(0); } await loadKbData(selectedKbId); notify(t("app.notify.documentTypeArchived")); }
    catch (error) { showError(error); }
  };
  const activateDocumentTemplate = async template => {
    try { await api(`/v1/document-templates/${template.id}/activate`, {method: "POST"}); await loadKbData(selectedKbId); notify(t("app.notify.documentTypeActivated")); }
    catch (error) { showError(error); }
  };
  const openDocument = async document => {
    try {
      const [preview, jobs] = await Promise.all([api(`/v1/documents/${document.id}/text`), api(`/v1/documents/${document.id}/jobs`)]);
      setDocumentJobPollError("");
      setDocumentPreview({...preview, title: document.title || document.original_filename}); setDocumentJobs(jobs);
    } catch (error) { showError(error); }
  };
  const closePreview = useCallback(() => setDocumentPreview(null), []);
  const reprocessDocument = async document => {
    try { await api(`/v1/documents/${document.id}/reprocess`, {method: "POST"}); await loadKbData(selectedKbId); notify(t("app.notify.documentQueuedForReprocessing")); }
    catch (error) { showError(error); }
  };
  const deleteDocument = async document => {
    if (!window.confirm(t("app.confirm.deleteDocument", {name: document.title || document.original_filename}))) return;
    try { await api(`/v1/documents/${document.id}`, {method: "DELETE"}); await loadKbData(selectedKbId); notify(t("app.notify.documentMovedToDeleted")); }
    catch (error) { showError(error); }
  };
  const restoreDocument = async document => {
    try { await api(`/v1/documents/${document.id}/restore`, {method: "POST"}); await loadKbData(selectedKbId, true); notify(t("app.notify.documentRestored")); }
    catch (error) { showError(error); }
  };
  const reindexEmbeddings = async () => {
    try { const result = await api(`/v1/knowledge-bases/${selectedKbId}/documents/reindex`, {method: "POST"}); await loadKbData(selectedKbId); notify(t("app.notify.embeddingReindexQueued", {count: result.count})); }
    catch (error) { showError(error); }
  };
  const loadAccess = async () => {
    const results = await Promise.allSettled([api("/v1/tokens"), api("/v1/audit-logs?limit=20")]);
    const [tokenResult, auditResult] = results;
    if (tokenResult.status === "fulfilled") setTokens(tokenResult.value);
    if (auditResult.status === "fulfilled") setAuditLogs(auditResult.value);
    const errors = results.filter(result => result.status === "rejected").map(result => result.reason?.message || t("app.error.accessDataRequestFailed"));
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
    if (user && userRoleLevel >= ROLE_LEVEL.admin && (activeView === "users" || activeView === "groups")) Promise.all([loadUsers(), loadGroups()]).catch(showError);
  }, [user, activeView]);
  useEffect(() => {
    const onKeyDown = event => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault(); setIsCommandPaletteOpen(open => !open);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
  const createMcpToken = async (payload, label = "MCP") => {
    const result = await api("/v1/tokens", {method: "POST", body: JSON.stringify(payload)});
    await loadAccess(); notify(t("app.notify.tokenCreated", {label})); return result;
  };
  const rotateMcpToken = async (tokenId, label = "MCP") => {
    const result = await api(`/v1/tokens/${tokenId}/rotate`, {method: "POST"});
    await loadAccess(); notify(t("app.notify.tokenRotated", {label})); return result;
  };
  const changeTokenState = async (tokenId, action) => {
    await api(`/v1/tokens/${tokenId}/${action}`, {method: "POST"}); await loadAccess();
    const state = t({enable: "app.tokenState.enabled", disable: "app.tokenState.disabled", revoke: "app.tokenState.revoked"}[action] || "app.tokenState.updated");
    notify(t("app.notify.tokenStateChanged", {state}));
  };
  const submitQueryFeedback = async (resultId, rating) => {
    try { await api(`/v1/query/results/${resultId}/feedback`, {method: "POST", body: JSON.stringify({rating})}); notify(t("app.notify.feedbackRecorded")); }
    catch (error) { showError(error); }
  };

  if (isSessionLoading) return <main className="login-page"><section className="login-card session-loading"><img className="login-logo" src="/logo-softnix.png" alt="Softnix"/><p className="eyebrow">{t("login.brand")}</p><h1>{t("app.sessionLoading.title")}</h1><p className="login-copy">{t("app.sessionLoading.body")}</p></section></main>;
  if (!user) return <Login onLogin={data => setUser(data.user)}/>;
  const switchView = view => {
    setActiveView(view); setDocumentPreview(null);
    setViewTrail(current => current.at(-1) === view ? current : pushViewTrail(current, view));
    writeWorkspaceRoute(view, selectedKbId);
  };
  const navigateToView = view => {
    setActiveView(view); setDocumentPreview(null);
    setViewTrail(current => pushViewTrail(current, view));
    writeWorkspaceRoute(view, selectedKbId);
  };
  const goBack = () => {
    const next = viewTrail.length > 1 ? viewTrail.slice(0, -1) : viewTrail;
    const view = next.at(-1) || "knowledge-bases";
    setViewTrail(next);
    setActiveView(view);
    setDocumentPreview(null);
    writeWorkspaceRoute(view, selectedKbId, {replace: true});
  };
  const sideNav = <SideNav header={<div className="brand-lockup"><img src="/logo-softnix.png" alt="Softnix"/><SideNavHeading superheading="SOFTNIX" heading="Knowledge Intelligence"/></div>} topContent={<Button label={t("sideNav.newKnowledgeBase")} variant="primary" onClick={() => switchView("knowledge-bases")}/>} collapsible ariaLabel={t("ui.primaryNavigation")} expandLabel={t("ui.expandNavigation")} collapseLabel={t("ui.collapseNavigation")}>
    <SideNavSection title={t("sideNav.category.knowledge")} subtitle={t("sideNav.category.knowledge.subtitle")} className="side-nav-category">
      <SideNavItem icon={<Database weight="regular"/>} label={t("workflow.knowledgeBases")} isSelected={activeView === "knowledge-bases"} onClick={() => switchView("knowledge-bases")}/>
      <SideNavItem icon={<FileText weight="regular"/>} label={t("workflow.documents")} isSelected={activeView === "documents"} onClick={() => switchView("documents")}/>
    </SideNavSection>
    <SideNavSection title={t("sideNav.category.insights")} subtitle={t("sideNav.category.insights.subtitle")} className="side-nav-category">
      <SideNavItem icon={<MagnifyingGlass weight="regular"/>} label={t("workflow.search")} isSelected={activeView === "search"} onClick={() => switchView("search")}/>
      <SideNavItem icon={<Compass weight="regular"/>} label={t("workflow.explore")} isSelected={activeView === "explore"} onClick={() => switchView("explore")}/>
    </SideNavSection>
    <SideNavSection title={t("sideNav.category.administration")} subtitle={t("sideNav.category.administration.subtitle")} className="side-nav-category"><SideNavItem icon={<Key weight="regular"/>} label={t("workflow.mcpTokens")} isSelected={activeView === "mcp-tokens"} onClick={() => switchView("mcp-tokens")}/><SideNavItem icon={<BookOpen weight="regular"/>} label={t("workflow.ingestTokens")} isSelected={activeView === "ingest-tokens"} onClick={() => switchView("ingest-tokens")}/>{userRoleLevel >= ROLE_LEVEL.manager && <SideNavItem icon={<ChartLineUp weight="regular"/>} label={t("workflow.logs")} isSelected={activeView === "logs"} onClick={() => switchView("logs")}/>}{userRoleLevel >= ROLE_LEVEL.admin && <><SideNavItem icon={<Users weight="regular"/>} label={t("workflow.users")} isSelected={activeView === "users"} onClick={() => switchView("users")}/><SideNavItem icon={<UsersThree weight="regular"/>} label={t("workflow.groups")} isSelected={activeView === "groups"} onClick={() => switchView("groups")}/></>}</SideNavSection>
  </SideNav>;
  const topNav = <TopNav label={t("app.workspaceNavAriaLabel")} menuLabel={t("ui.openNavigation")} commandLabel={t("ui.openCommandPalette")} heading={<TopNavHeading heading={selectedKb?.name || t("app.workspaceTitle")}/>} endContent={<div className="topnav-user"><Button label={language === "th" ? "EN" : "TH"} size="sm" variant="ghost" onClick={() => setLanguage(language === "th" ? "en" : "th")} aria-label={t("app.toggleLanguage")}/><span className="status-indicator"/> <button type="button" className="topnav-profile-button" onClick={() => switchView("profile")} title={t("workflow.profile")}>{user.username}</button></div>}/>;
  const commandItems = [
    ["knowledge-bases", Database, t("workflow.knowledgeBases")], ["documents", FileText, t("workflow.documents")], ["search", MagnifyingGlass, t("workflow.search")], ["explore", Compass, t("workflow.explore")], ["mcp-tokens", Key, t("workflow.mcpTokens")], ["ingest-tokens", BookOpen, t("workflow.ingestTokens")],
    ...(userRoleLevel >= ROLE_LEVEL.manager ? [["logs", ChartLineUp, t("workflow.logs")]] : []),
    ...(userRoleLevel >= ROLE_LEVEL.admin ? [["users", Users, t("workflow.users")], ["groups", UsersThree, t("workflow.groups")]] : []),
    ["profile", User, t("workflow.profile")],
  ].map(([id, Icon, label]) => ({id, label, icon: <Icon size={16}/>, group: t("app.workspaceTitle"), onSelect: () => switchView(id)}));
  commandItems.push(...kbs.map(kb => ({id: `kb-${kb.id}`, label: kb.name, icon: <Database size={16}/>, group: t("workflow.knowledgeBases"), onSelect: () => { setSelectedKbId(kb.id); switchView("knowledge-bases"); }})));

  return <Theme><AppShell topNav={topNav} sideNav={sideNav} onCommand={() => setIsCommandPaletteOpen(true)} closeNavigationLabel={t("ui.closeNavigation")}>
    <div className="workspace" aria-live="polite">
      {message && <Toast body={message.body} type={message.type} isAutoHide={message.type !== "error"} autoHideDuration={5000} dismissLabel={t("ui.dismissNotification")} onDismiss={() => setMessage(null)}/>}
      <CommandPalette open={isCommandPaletteOpen} onClose={closeCommandPalette} items={commandItems} title={t("app.workspaceNavAriaLabel")} searchPlaceholder={t("ui.commandSearchPlaceholder")} searchLabel={t("ui.commandSearchLabel")} noMatchLabel={t("ui.commandNoMatch")}/>
      <WorkflowNavigation activeView={activeView} selectedKb={selectedKb} hasCompletedDocuments={hasCompletedDocuments} viewTrail={viewTrail} onNavigate={navigateToView} onBack={goBack} onNavigateNext={switchView}/>
      {activeView === "knowledge-bases" && <KnowledgeBases kbs={kbs} selectedKbId={selectedKbId} setSelectedKbId={setSelectedKbId} newKbName={newKbName} setNewKbName={setNewKbName} createKb={createKb} manageKnowledgeBase={manageKnowledgeBase} updateRetrievalConfig={updateRetrievalConfig} updateKnowledgeBaseIcon={updateKnowledgeBaseIcon} onContinue={() => switchView("documents")}/>}
      {activeView === "documents" && (
        <Documents selectedKb={selectedKb} documents={documents} documentTotal={documentTotal} documentOffset={documentOffset} setDocumentOffset={setDocumentOffset} documentSearch={documentSearch} setDocumentSearch={setDocumentSearch} documentStatusFilter={documentStatusFilter} setDocumentStatusFilter={setDocumentStatusFilter} documentTypeFilter={documentTypeFilter} setDocumentTypeFilter={setDocumentTypeFilter} documentsLoading={documentsLoading} hasCompletedDocuments={hasCompletedDocuments} showDeletedDocuments={showDeletedDocuments} setShowDeletedDocuments={setShowDeletedDocuments} uploadFile={uploadFile} setUploadFile={setUploadFile} uploadTitle={uploadTitle} setUploadTitle={setUploadTitle} uploadDocumentType={uploadDocumentType} setUploadDocumentType={setUploadDocumentType} documentTemplates={documentTemplates} uploadTemplateId={uploadTemplateId} setUploadTemplateId={setUploadTemplateId} uploadMetadata={uploadMetadata} setUploadMetadata={setUploadMetadata} createDocumentTemplate={createDocumentTemplate} updateDocumentTemplate={updateDocumentTemplate} deactivateDocumentTemplate={deactivateDocumentTemplate} activateDocumentTemplate={activateDocumentTemplate} uploadDocument={uploadDocument} isUploading={isUploading} openDocument={openDocument} extractLegalMetadata={extractLegalMetadata} saveLegalMetadata={saveLegalMetadata} deleteLegalMetadata={deleteLegalMetadata} saveDocumentMetadata={saveDocumentMetadata} reprocessDocument={reprocessDocument} deleteDocument={deleteDocument} restoreDocument={restoreDocument} reindexEmbeddings={reindexEmbeddings} refreshDocuments={() => loadKbData(selectedKbId).catch(showError)} documentPreview={documentPreview} documentJobs={documentJobs} documentJobPolling={documentJobPolling} documentJobPollError={documentJobPollError} legalInstruments={legalInstruments} resolveLegalRegistry={resolveLegalRegistry} updateLegalInstrument={updateLegalInstrument} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship} onClosePreview={closePreview} onCreateKb={() => switchView("knowledge-bases")} onSearch={() => switchView("search")} onExplore={() => switchView("explore")}/>
      )}
      {activeView === "search" && (
        <SearchView selectedKb={selectedKb} documents={documents} completedDocuments={hasCompletedDocuments} query={query} setQuery={setQuery} queryAsOfDate={queryAsOfDate} setQueryAsOfDate={setQueryAsOfDate} queryIncludeHistorical={queryIncludeHistorical} setQueryIncludeHistorical={setQueryIncludeHistorical} runQuery={runQuery} isQuerying={isQuerying} queryResult={queryResult} submitFeedback={submitQueryFeedback} onDocuments={() => switchView("documents")} onOpenSource={document => { switchView("documents"); openDocument(document); }}/>
      )}
      {activeView === "explore" && (
        <ExploreView selectedKb={selectedKb} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={() => loadKbData(selectedKbId).catch(showError)} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship} resolveLegalRegistry={resolveLegalRegistry} onOpenDocument={document => { switchView("documents"); openDocument(document); }}/>
      )}
      {activeView === "users" && userRoleLevel >= ROLE_LEVEL.admin && (
        <UsersView users={users} groups={groups} loadUsers={loadUsers} loadGroups={loadGroups} notify={notify} showError={showError}/>
      )}
      {activeView === "groups" && userRoleLevel >= ROLE_LEVEL.admin && (
        <GroupsView groups={groups} users={users} loadGroups={loadGroups} notify={notify} showError={showError}/>
      )}
      {activeView === "profile" && (
        <ProfileView me={user} notify={notify} showError={showError}/>
      )}
      {activeView === "mcp-tokens" && <McpTokensView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} loadAccess={loadAccess} createMcpToken={createMcpToken} rotateMcpToken={rotateMcpToken} changeTokenState={changeTokenState}/>}
      {activeView === "ingest-tokens" && <IngestTokensView selectedKb={selectedKb} knowledgeBases={kbs} tokens={tokens} auditLogs={auditLogs} loadAccess={loadAccess} createMcpToken={createMcpToken} rotateMcpToken={rotateMcpToken} changeTokenState={changeTokenState}/>}
      {activeView === "logs" && <LoggingView transactions={transactionLogs} traces={traceLogs} loadTransactions={loadTransactionLogs} loadTraces={loadTraceLogs} hasMoreTransactions={Boolean(transactionCursor)} hasMoreTraces={Boolean(traceCursor)}/>}
    </div>
  </AppShell></Theme>;
}

function LoggingView({transactions, traces, loadTransactions, loadTraces, hasMoreTransactions, hasMoreTraces}) {
  const {t} = useLanguage();
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
  return <><PageHeading eyebrow={t("logging.eyebrow")} title={t("logging.title")} description={t("logging.description")} actions={<><label className="log-auto-refresh"><input type="checkbox" checked={autoRefresh} onChange={event => setAutoRefresh(event.target.checked)}/> {t("logging.autoRefresh")}</label><Button label={t("logging.refreshLogs")} variant="secondary" onClick={() => Promise.all([loadTransactions(), loadTraces()])}/></>}/>
    <section className="metric-grid"><Metric value={transactions.length} label={t("logging.metrics.recentTransactions")} detail={t("logging.metrics.recentTransactionsDetail")}/><Metric value={errors} label={t("logging.metrics.errors")} detail={t("logging.metrics.errorsDetail")}/><Metric value={retrievalRequests} label={t("logging.metrics.retrievalExecutions")} detail={t("logging.metrics.retrievalExecutionsDetail", {count: mcpRequests})}/><Metric value={`${averageDuration} ms`} label={t("logging.metrics.averageDuration")} detail={t("logging.metrics.averageDurationDetail")}/></section>
    <div className="log-tabs" role="tablist"><button role="tab" aria-selected={view === "traces"} className={view === "traces" ? "selected" : ""} onClick={() => setView("traces")}>{t("logging.tabs.traceExplorer")}</button><button role="tab" aria-selected={view === "transactions"} className={view === "transactions" ? "selected" : ""} onClick={() => setView("transactions")}>{t("logging.tabs.allTransactions")}</button></div>
    {view === "traces" ? <><TraceExplorer traces={traces}/>{hasMoreTraces && <div className="log-load-more"><Button label={t("logging.loadOlderTraces")} variant="secondary" onClick={() => loadTraces(true)}/></div>}</> : <Card padding={4}><div className="log-filter-bar"><TextInput label={t("logging.filter.findLabel")} value={search} onChange={setSearch} placeholder={t("logging.filter.findPlaceholder")}/><Selector label={t("logging.filter.methodLabel")} value={method} onChange={setMethod} options={[{value: "all", label: t("logging.filter.allMethods")}, ...methods.map(value => ({value, label: value}))]}/><Selector label={t("common.status")} value={status} onChange={setStatus} options={[{value: "all", label: t("common.allStatuses")}, {value: "2", label: t("logging.filter.status2xx")}, {value: "4", label: t("logging.filter.status4xx")}, {value: "5", label: t("logging.filter.status5xx")}, {value: "error", label: t("logging.filter.allErrors")}]}/></div>
      <p className="section-copy log-privacy-note">{t("logging.privacyNote")}</p>
      {visible.length ? <div className="transaction-list">{visible.map(item => {
        const isOpen = expandedId === item.id;
        const isError = Number(item.status_code) >= 400;
        const execution = item.retrieval;
        return <article className={`transaction-row ${isError ? "has-error" : ""}`} key={item.id}><button type="button" className="transaction-summary" onClick={() => setExpandedId(isOpen ? null : item.id)} aria-expanded={isOpen}><span className="transaction-route"><b className={`http-method ${item.method?.toLowerCase()}`}>{item.method}</b><code>{item.path}</code><small>{new Date(item.created_at).toLocaleString()} · {item.authentication}{execution ? t("logging.transaction.retrievalTraceSuffix") : ""}</small></span><span className={`transaction-status ${isError ? "error" : ""}`}>{item.status_code}</span><span className="transaction-duration">{item.duration_ms} ms</span></button>{isOpen && <div className="transaction-detail"><div><span>{t("logging.transaction.requestIdLabel")}</span><code>{item.request_id}</code></div><div><span>{t("logging.transaction.transactionLabel")}</span><code>{item.method} {item.path} → {item.status_code} in {item.duration_ms} ms</code></div>{execution && <RetrievalExecutionTrace execution={execution}/>}<p>{t("logging.transaction.correlateNote")}</p></div>}</article>;
      })}</div> : <EmptyState isCompact title={t("logging.emptyTransactions.title")} description={t("logging.emptyTransactions.description")}/>}{hasMoreTransactions && <div className="log-load-more"><Button label={t("logging.loadOlderTransactions")} variant="secondary" onClick={() => loadTransactions(true)}/></div>}
    </Card>}</>;
}

function RetrievalExecutionTrace({execution}) {
  const {t} = useLanguage();
  const plan = execution.retrieval_plan || {};
  const trace = execution.retrieval_trace || [];
  return <section className="retrieval-execution"><div className="retrieval-execution-heading"><span>{t("trace.execution.heading")}</span><b>{plan.intent || execution.tool || t("trace.execution.defaultIntent")}</b></div>{plan.channels?.length > 0 && <p className="retrieval-plan-summary">{plan.planner_source || t("trace.execution.defaultPlannerSource")} · {plan.channels.join(", ")}{plan.fallback_reason ? t("trace.execution.deterministicFallbackSuffix") : ""}</p>}<ul className="retrieval-execution-list">{trace.map((step, index) => <li key={`${step.channel}-${step.system}-${index}`}><i className={`mcp-step-dot ${step.status}`}/><div><b>{step.channel}</b><small>{t("trace.execution.stepSummary", {system: step.system, status: step.status, count: step.result_count || 0, duration: step.duration_ms || 0})}</small>{step.detail && <span>{step.detail}</span>}</div></li>)}</ul></section>;
}

function TraceExplorer({traces}) {
  const {t} = useLanguage();
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
  return <section className="trace-explorer"><aside className="trace-list-pane"><div className="trace-list-heading"><div><p className="eyebrow">{t("trace.explorer.eyebrow")}</p><h2>{t("trace.explorer.heading")}</h2></div><span>{visible.length}</span></div><TextInput label={t("trace.explorer.findLabel")} value={filter} onChange={setFilter} placeholder={t("trace.explorer.findPlaceholder")}/>
    <div className="trace-list">{visible.map(item => <button type="button" key={item.trace_id} className={`trace-summary ${item.trace_id === selectedTraceId ? "selected" : ""}`} onClick={() => setSelectedTraceId(item.trace_id)}><span className={`trace-status-dot ${item.status}`}/><span><b>{item.tool || t("trace.explorer.defaultTool")}</b><small>{item.intent || t("trace.execution.defaultIntent")} · {item.transport}</small><small>{new Date(item.created_at).toLocaleString()}</small></span><em>{item.duration_ms} ms</em></button>)}</div>
  </aside><main className="trace-detail-pane">{loading ? <p className="section-copy">{t("trace.explorer.loading")}</p> : trace ? <TraceWaterfall trace={trace}/> : <EmptyState isCompact title={t("trace.explorer.emptyTitle")} description={t("trace.explorer.emptyDescription")}/>}</main></section>;
}

function TraceStatusLabel(t, {status, reasonCode}) {
  if (status === "skipped") return reasonCode === "not_selected_by_plan" ? t("trace.status.skippedByPlan") : t("trace.status.skippedUnavailable");
  if (status === "unavailable") return t("trace.status.failedUnavailable");
  if (status === "used") return t("trace.status.completed");
  return status || t("trace.status.unknown");
}

function TraceOverview({trace}) {
  const {t} = useLanguage();
  const request = trace.request_summary || {};
  const response = trace.response_summary || {};
  return <div className="trace-overview-grid">
    <section className="trace-payload-card"><p className="eyebrow">{t("trace.overview.request")}</p><div className="trace-query-preview">{request.query_preview || t("trace.overview.queryPreviewUnavailable")}</div><dl className="trace-meta-list"><div><dt>{t("trace.overview.lengthLabel")}</dt><dd>{t("trace.overview.lengthValue", {count: request.query_length || 0})}</dd></div><div><dt>{t("trace.overview.sha256Label")}</dt><dd><code>{request.query_sha256 || "—"}</code></dd></div><div><dt>{t("trace.overview.filtersLabel")}</dt><dd>{Object.keys(request.filter_summary || {}).length ? JSON.stringify(request.filter_summary) : t("common.none")}</dd></div></dl><p className="trace-safe-note">{t("trace.overview.safeNoteRequest")}</p></section>
    <section className="trace-payload-card"><p className="eyebrow">{t("trace.overview.response")}</p><div className="trace-answer-preview">{response.answer_preview || t("trace.overview.noAnswerPreview")}</div><dl className="trace-meta-list"><div><dt>{t("common.status")}</dt><dd>{response.status || trace.status}</dd></div><div><dt>{t("trace.overview.sourcesLabel")}</dt><dd>{t("trace.overview.sourcesValue", {count: response.source_count ?? trace.source_count ?? 0})}</dd></div><div><dt>{t("trace.overview.entitiesRelationshipsLabel")}</dt><dd>{response.entity_count ?? 0} / {response.relationship_count ?? 0}</dd></div></dl><p className="trace-safe-note">{t("trace.overview.safeNoteResponse")}</p></section>
  </div>;
}

function TraceDecision({trace}) {
  const {t} = useLanguage();
  const plan = trace.retrieval_plan || {};
  return <section className="trace-decision"><div className="trace-decision-row"><span>{t("trace.decision.intentLabel")}</span><strong>{plan.intent || trace.intent || t("trace.execution.defaultIntent")}</strong></div><div className="trace-decision-row"><span>{t("trace.decision.sourceLabel")}</span><strong>{plan.planner_source || t("trace.execution.defaultPlannerSource")}{plan.policy_version ? t("trace.decision.policyVersionSuffix", {version: plan.policy_version}) : ""}</strong></div><div className="trace-decision-row"><span>{t("trace.decision.whyLabel")}</span><strong>{plan.rationale || t("trace.decision.noRationale")}</strong></div><div className="trace-decision-row"><span>{t("trace.decision.channelsLabel")}</span><strong>{plan.channels?.length ? plan.channels.join(t("trace.decision.channelsJoinedBy")) : t("common.none")}</strong></div><div className="trace-decision-row"><span>{t("trace.decision.limitsLabel")}</span><strong>{t("trace.decision.limitsValue", {maxSources: plan.max_sources || "—", graphDepth: plan.graph_depth || "—", scope: plan.graph_scope || t("trace.decision.scopeNone")})}</strong></div>{plan.fallback_reason && <div className="trace-decision-warning">{t("trace.decision.fallbackPrefix")}{plan.fallback_reason}</div>}<p className="trace-safe-note">{t("trace.decision.skippedNote")}</p>{plan.channels?.includes("graph") && <p className="trace-safe-note">{t("trace.decision.neo4jNote")}</p>}</section>;
}

function TraceTimeline({trace}) {
  const {t} = useLanguage();
  const total = Math.max(trace.duration_ms || 0, 1);
  const spans = (trace.spans || []).map(span => {
    const left = Math.min(100, (Number(span.offset_ms || 0) / total) * 100);
    const width = Math.max(1.5, Math.min(100 - left, (Number(span.duration_ms || 0) / total) * 100));
    return <details className="waterfall-row" key={span.span_id}>
      <summary><div><b>{span.channel}</b><small>{span.system}</small></div><div className="waterfall-track"><span className={`waterfall-bar ${span.status}`} style={{left: `${left}%`, width: `${width}%`}} title={`${span.offset_ms}–${Number(span.offset_ms || 0) + Number(span.duration_ms || 0)} ms`}/></div><div><em>{span.duration_ms} ms</em><small>{TraceStatusLabel(t, {status: span.status, reasonCode: span.reason_code})}{t("trace.timeline.resultsSuffix", {count: span.result_count || 0})}</small></div></summary>
      {span.detail && <p>{span.detail}</p>}
      <div className="trace-span-details"><dl><div><dt>{t("trace.timeline.inputLabel")}</dt><dd>{t("trace.timeline.inputSummary", {sha: span.input_summary?.query_sha256 ? t("trace.timeline.shaValue", {sha: span.input_summary.query_sha256.slice(0, 12)}) : t("trace.timeline.summaryUnavailable"), kbCount: span.input_summary?.knowledge_base_count ?? 0, maxSources: span.input_summary?.max_sources ?? "—"})}</dd></div><div><dt>{t("trace.timeline.outputLabel")}</dt><dd>{t("trace.timeline.outputSummary", {count: span.output_summary?.result_count ?? span.result_count ?? 0, status: span.output_summary?.status || span.status})}</dd></div>{span.reason_code && <div><dt>{t("trace.timeline.reasonLabel")}</dt><dd>{TraceStatusLabel(t, {status: span.status, reasonCode: span.reason_code})}</dd></div>}</dl></div>
    </details>;
  });
  return <section className="waterfall-panel"><div className="waterfall-heading"><div>{t("trace.timeline.executionSpans")}</div><span>0 ms</span><span>{trace.duration_ms} ms</span></div><div className="waterfall-root"><b>{trace.root_span?.name || t("trace.timeline.requestFallback")}</b><span className={`waterfall-root-bar ${trace.status}`}/><em>{trace.duration_ms} ms</em></div><div className="waterfall-spans">{spans}</div></section>;
}

function TraceEvidence({trace}) {
  const {t} = useLanguage();
  const ids = trace.response_summary?.citation_ids || [];
  return <section className="trace-evidence"><p className="eyebrow">{t("trace.evidence.citations")}</p><h3>{ids.length ? t("trace.evidence.citedSources", {count: ids.length}) : t("trace.evidence.noCitations")}</h3>{ids.length ? <div className="trace-chip-list">{ids.map(id => <span className="trace-chip" key={id}>{id}</span>)}</div> : <p className="trace-empty-note">{t("trace.evidence.noEvidenceNote")}</p>}<p className="trace-safe-note">{t("trace.evidence.safeNote")}</p></section>;
}

function TraceWaterfall({trace}) {
  const {t} = useLanguage();
  const [tab, setTab] = useState("overview");
  const plan = trace.retrieval_plan || {};
  const tabs = [["overview", t("trace.waterfall.tabs.overview")], ["decision", t("trace.waterfall.tabs.decision")], ["timeline", t("trace.waterfall.tabs.timeline")], ["evidence", t("trace.waterfall.tabs.evidence")]];
  return <div className="trace-waterfall">
    <div className="trace-detail-heading"><div><p className="eyebrow">{trace.transport === "mcp" ? t("trace.waterfall.mcpTrace") : t("trace.waterfall.searchTrace")}</p><h2>{trace.root_span?.name || t("trace.waterfall.defaultName")}</h2><p>{t("trace.waterfall.summaryLine", {intent: trace.intent || t("trace.execution.defaultIntent"), count: trace.source_count, duration: trace.duration_ms})}</p></div><span className={`trace-status ${trace.status}`}>{trace.status}</span></div>
    <section className="trace-context"><div><span>{t("trace.waterfall.traceIdLabel")}</span><code>{trace.trace_id}</code></div><div><span>{t("trace.waterfall.scopeLabel")}</span><code>{t("trace.waterfall.scopeValue", {count: trace.knowledge_base_ids?.length || 0})}</code></div><div><span>{t("trace.waterfall.plannerLabel")}</span><code>{plan.planner_source || t("trace.execution.defaultPlannerSource")}</code></div></section>
    <nav className="trace-tabs" aria-label={t("trace.waterfall.tabsAriaLabel")}>{tabs.map(([value, label]) => <button type="button" role="tab" aria-selected={tab === value} className={tab === value ? "selected" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}</nav>
    {tab === "overview" && <TraceOverview trace={trace}/>} {tab === "decision" && <TraceDecision trace={trace}/>} {tab === "timeline" && <TraceTimeline trace={trace}/>} {tab === "evidence" && <TraceEvidence trace={trace}/>}
  </div>;
}

const PageHeading = ({eyebrow, title, description, actions}) => <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{description}</p></div>{actions && <div className="page-actions">{actions}</div>}</div>;
const Drawer = ({title, onClose, children}) => <div className="document-type-drawer-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside className="document-type-drawer" role="dialog" aria-modal="true" onMouseDown={event => event.stopPropagation()}><header className="document-type-drawer-header"><div><h2>{title}</h2></div><button type="button" className="document-type-drawer-close" onClick={onClose} aria-label="Close">✕</button></header><div className="drawer-body">{children}</div></aside></div>;

const WORKFLOW_KEYS = {
  "knowledge-bases": "workflow.knowledgeBases",
  documents: "workflow.documents",
  search: "workflow.search",
  explore: "workflow.explore",
  "mcp-tokens": "workflow.mcpTokens",
  "ingest-tokens": "workflow.ingestTokens",
  logs: "workflow.logs",
};
const isAdministrationView = view => view === "mcp-tokens" || view === "ingest-tokens" || view === "logs";
// Bound how far "Back" can retrace: enough to undo a few steps without the
// history growing without limit as the user bounces between views.
const MAX_VIEW_TRAIL = 4;
const pushViewTrail = (current, view) => {
  const index = current.lastIndexOf(view);
  const next = index >= 0 ? current.slice(0, index + 1) : [...current, view];
  return next.length > MAX_VIEW_TRAIL ? next.slice(-MAX_VIEW_TRAIL) : next;
};

function WorkflowNavigation({activeView, selectedKb, hasCompletedDocuments, viewTrail, onNavigate, onBack, onNavigateNext}) {
  const {t} = useLanguage();
  const workflowLabel = view => view === "administration" ? t("nav.administration") : t(WORKFLOW_KEYS[view]);
  // Breadcrumbs describe the current information architecture, not the route
  // history. The latter is reserved for the Back control so navigation stays
  // legible after visiting several screens.
  const breadcrumbViews = activeView === "knowledge-bases" ? ["knowledge-bases"]
    : isAdministrationView(activeView) ? ["administration", activeView]
      : ["knowledge-bases", activeView];
  const previousView = viewTrail.length > 1 ? viewTrail[viewTrail.length - 2] : null;
  const previousLabel = previousView ? workflowLabel(previousView) : null;
  const nextView = activeView === "knowledge-bases" ? "documents"
    : activeView === "documents" && hasCompletedDocuments ? "search"
      : activeView === "search" && hasCompletedDocuments ? "explore"
        : activeView === "mcp-tokens" ? "ingest-tokens"
          : activeView === "ingest-tokens" ? "logs" : null;
  const nextLabel = nextView ? workflowLabel(nextView) : null;
  const canNavigateNext = nextView && (nextView !== "documents" || Boolean(selectedKb)) && (nextView !== "search" && nextView !== "explore" || Boolean(selectedKb && hasCompletedDocuments));
  const backLabel = previousLabel ? t("nav.backTo", {label: previousLabel}) : t("nav.back");

  return <nav className="workflow-navigation" aria-label={t("nav.workflowAriaLabel")}>
    <div className="workflow-navigation-inner">
      <div className="workflow-navigation-path">
        <button type="button" className="workflow-back" onClick={onBack} disabled={!previousView} aria-label={backLabel} title={backLabel}>
          <span aria-hidden="true">←</span> {t("nav.back")}
        </button>
        <ol className="workflow-breadcrumbs">
          {breadcrumbViews.map((view, index) => <li key={`${view}-${index}`}>
            {index < breadcrumbViews.length - 1 && view !== "administration" ? <button type="button" onClick={() => onNavigate(view)}>{workflowLabel(view)}</button> : <span aria-current={index === breadcrumbViews.length - 1 ? "page" : undefined}>{workflowLabel(view)}</span>}
          </li>)}
        </ol>
        {selectedKb && activeView !== "knowledge-bases" && <span className="workflow-context" title={selectedKb.name}>{selectedKb.name}</span>}
      </div>
      {canNavigateNext && <button type="button" className="workflow-next" onClick={() => onNavigateNext(nextView)}>{nextLabel}<span aria-hidden="true">→</span></button>}
    </div>
  </nav>;
}
const STATUS_KEYS = ["queued", "extracting", "indexing", "completed", "failed", "ocr_required", "disabled"];
const STATUS_HELP_KEYS = ["queued", "extracting", "indexing", "failed", "ocr_required"];
const statusHelp = (t, status) => STATUS_HELP_KEYS.includes(status) ? t(`status.${status}.help`) : null;
const StatusBadge = ({status}) => {
  const {t} = useLanguage();
  const label = STATUS_KEYS.includes(status) ? t(`status.${status}.label`) : status.replace(/_/g, " ");
  return <Badge label={label} variant={status === "completed" ? "success" : status === "failed" || status === "ocr_required" ? "error" : status === "queued" || status === "extracting" || status === "indexing" ? "warning" : "neutral"}/>;
};
const Metric = ({value, label, detail}) => <Card padding={3}><p className="metric-value">{value}</p><p className="metric-label">{label}</p>{detail && <p className="metric-detail">{detail}</p>}</Card>;

const KB_ICON_OPTIONS = [
  {id: "auto", Icon: Database}, {id: "database", Icon: Database}, {id: "book", Icon: BookOpen},
  {id: "document", Icon: FileText}, {id: "policy", Icon: ShieldCheck}, {id: "legal", Icon: Scales},
  {id: "court", Icon: Gavel}, {id: "agency", Icon: Buildings},
];
const KB_ICON_COMPONENTS = Object.fromEntries(KB_ICON_OPTIONS.map(option => [option.id, option.Icon]));

const inferredKnowledgeBaseIcon = name => {
  const value = (name || "").toLocaleLowerCase();
  if (/(คำพิพากษา|ศาล|court|judgment)/.test(value)) return "court";
  if (/(กฎหมาย|กฏหมาย|กฎ|กฏ|พระราช|ระเบียบ|ข้อบังคับ|legal|law|regulation)/.test(value)) return "legal";
  if (/(pdpa|privacy|security|ความปลอดภัย|นโยบาย|policy|it\b|ไอที)/.test(value)) return "policy";
  if (/(กระทรวง|กรม|หน่วยงาน|agency|government)/.test(value)) return "agency";
  if (/(ประกาศ|หนังสือเวียน|คู่มือ|หนังสือ|manual|guide|reference)/.test(value)) return "book";
  if (/(บันทึก|เอกสาร|document|contract|agreement)/.test(value)) return "document";
  return "database";
};

function KnowledgeBaseIcon({knowledgeBase, icon, size = 20, className}) {
  const resolvedIcon = icon === "auto" ? inferredKnowledgeBaseIcon(knowledgeBase?.name) : (icon || (knowledgeBase?.icon && knowledgeBase.icon !== "auto" ? knowledgeBase.icon : inferredKnowledgeBaseIcon(knowledgeBase?.name)));
  const Icon = KB_ICON_COMPONENTS[resolvedIcon] || Database;
  return <Icon className={className} size={size} weight="duotone" aria-hidden="true"/>;
}

function KnowledgeBaseIconPicker({knowledgeBase, onChange}) {
  const {t} = useLanguage();
  const pickerRef = useRef(null);
  const [isSaving, setIsSaving] = useState(false);
  const selectedIcon = knowledgeBase.icon || "auto";
  const selectIcon = async icon => {
    if (icon === selectedIcon || isSaving) return;
    setIsSaving(true);
    const saved = await onChange(knowledgeBase, icon);
    setIsSaving(false);
    if (saved && pickerRef.current) pickerRef.current.open = false;
  };
  return <details ref={pickerRef} className="kb-icon-picker"><summary><KnowledgeBaseIcon knowledgeBase={knowledgeBase} size={16}/><span>{t("kb.icon.change")}</span></summary><div className="kb-icon-palette" role="group" aria-label={t("kb.icon.pickerLabel", {name: knowledgeBase.name})}>{KB_ICON_OPTIONS.map(option => <button key={option.id} type="button" className={option.id === selectedIcon ? "selected" : ""} aria-label={t(`kb.icon.${option.id}`)} title={t(`kb.icon.${option.id}`)} aria-pressed={option.id === selectedIcon} disabled={isSaving} onClick={() => selectIcon(option.id)}><KnowledgeBaseIcon knowledgeBase={knowledgeBase} icon={option.id} size={18}/></button>)}</div></details>;
}

function KnowledgeBases({kbs, selectedKbId, setSelectedKbId, newKbName, setNewKbName, createKb, manageKnowledgeBase, updateRetrievalConfig, updateKnowledgeBaseIcon, onContinue}) {
  const {t} = useLanguage();
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
      <p className="eyebrow">{t("kb.hero.eyebrow")}</p>
      <h1>{t("kb.hero.title")}</h1>
      <p>{t("kb.hero.description")}</p>
      <div className="kb-hero-search">
        <TextInput label={t("kb.hero.searchLabel")} value={searchTerm} onChange={setSearchTerm} placeholder={t("kb.hero.searchPlaceholder")}/>
        <label className="native-field">{t("common.status")}<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">{t("common.allStatuses")}</option><option value="active">{t("common.active")}</option><option value="draft">{t("kb.status.draft")}</option><option value="disabled">{t("status.disabled.label")}</option></select></label>
      </div>
    </section>
    <div className="kb-hub-heading"><h2>{t("kb.hub.heading")}</h2><p>{kbs.length ? t("kb.hub.shownCount", {shown: visibleKnowledgeBases.length, total: kbs.length}) : t("kb.hub.createFirst")}</p></div>
    <section className="kb-hub-grid">
      {visibleKnowledgeBases.map(kb => <article className={`kb-hub-card ${kb.id === selectedKbId ? "selected" : ""}`} key={kb.id}>
        <button type="button" className="kb-hub-open" onClick={() => openKnowledgeBase(kb)}>
          <span className="kb-hub-avatar"><KnowledgeBaseIcon knowledgeBase={kb} size={22}/></span>
          <span className="kb-hub-title">{kb.name}</span>
          <span className="kb-hub-code">{kb.code}</span>
          <StatusBadge status={kb.status}/>
          <span className="kb-hub-link">{t("kb.hub.open")}</span>
        </button>
        <div className="kb-hub-actions">
          <KnowledgeBaseIconPicker knowledgeBase={kb} onChange={updateKnowledgeBaseIcon}/>
          {kb.status === "active" ? <Button label={t("common.disable")} size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "disable")}/> : <Button label={t("common.activate")} size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "activate")}/>}
          <Button label={t("common.delete")} size="sm" variant="ghost" onClick={() => manageKnowledgeBase(kb, "delete")}/>
        </div>
        <RetrievalPolicyEditor knowledgeBase={kb} onSave={config => updateRetrievalConfig(kb, config)}/>
      </article>)}
      <article className={`kb-hub-card kb-hub-create ${isCreating ? "open" : ""}`}>
        {isCreating
          ? <form className="form-stack" onSubmit={submitCreate}><TextInput label={t("kb.hub.create.nameLabel")} value={newKbName} onChange={setNewKbName} placeholder={t("kb.hub.create.namePlaceholder")} isRequired hasAutoFocus/><div className="kb-hub-create-actions"><Button label={t("common.cancel")} type="button" variant="ghost" size="sm" onClick={() => setIsCreating(false)}/><Button label={t("common.create")} type="submit" variant="primary" size="sm"/></div></form>
          : <button type="button" className="kb-hub-create-trigger" onClick={() => setIsCreating(true)}><span className="kb-hub-create-icon">+</span><span>{t("sideNav.newKnowledgeBase")}</span></button>}
      </article>
    </section>
    {kbs.length > 0 && !visibleKnowledgeBases.length && <EmptyState isCompact title={t("kb.hub.noMatch.title")} description={t("kb.hub.noMatch.description")}/>}
  </>;
}

function RetrievalPolicyEditor({knowledgeBase, onSave}) {
  const {t} = useLanguage();
  const current = knowledgeBase.retrieval_config || {};
  const [draft, setDraft] = useState(current);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(current), [knowledgeBase.id, knowledgeBase.retrieval_config]);
  const toggle = key => setDraft(value => ({...value, [key]: !value[key]}));
  const save = async event => { event.preventDefault(); setSaving(true); try { await onSave(draft); } finally { setSaving(false); } };
  return <details className="retrieval-policy"><summary>{t("retrievalPolicy.summary")}</summary><form className="retrieval-policy-form" onSubmit={save}><p className="section-copy">{t("retrievalPolicy.description")}</p><Selector label={t("retrievalPolicy.mode")} value={draft.retrieval_mode || "auto"} onChange={value => setDraft({...draft, retrieval_mode: value})} options={[{value: "auto", label: t("retrievalPolicy.mode.auto")}, {value: "balanced", label: t("retrievalPolicy.mode.balanced")}, {value: "precision", label: t("retrievalPolicy.mode.precision")}, {value: "recall", label: t("retrievalPolicy.mode.recall")}]}/><div className="policy-checks">{[["enable_vector","retrievalPolicy.check.vector"],["enable_fulltext","retrievalPolicy.check.fulltext"],["enable_graph","retrievalPolicy.check.graph"],["enable_lightrag","retrievalPolicy.check.lightrag"],["enable_reranker","retrievalPolicy.check.reranker"],["planner_llm_fallback","retrievalPolicy.check.llmFallback"]].map(([key,labelKey]) => <DesignSystemCheckbox key={key} label={t(labelKey)} checked={draft[key] !== false} onChange={() => toggle(key)}/>)}</div><div className="policy-numbers"><label>{t("retrievalPolicy.topK")}<input type="number" min="1" max="30" value={draft.default_top_k || 12} onChange={event => setDraft({...draft, default_top_k: Number(event.target.value)})}/></label><label>{t("retrievalPolicy.graphDepth")}<input type="number" min="1" max="3" value={draft.maximum_graph_depth || 3} onChange={event => setDraft({...draft, maximum_graph_depth: Number(event.target.value)})}/></label></div><Button label={t("retrievalPolicy.save")} type="submit" size="sm" variant="secondary" isLoading={saving}/></form></details>;
}

function MetadataFields({fields = [], values = {}, onChange, isDisabled = false}) {
  const {t} = useLanguage();
  const setValue = (key, value) => onChange({...values, [key]: value});
  if (!fields.length) return null;
  return <div className="metadata-field-grid">{fields.map(field => {
    const value = values[field.key] ?? (field.field_type === "boolean" ? false : "");
    const label = field.required ? t("metadataFields.requiredLabel", {label: field.label}) : field.label;
    if (field.field_type === "textarea") return <TextArea key={field.key} label={label} value={value} onChange={next => setValue(field.key, next)} rows={3} description={field.help_text} isDisabled={isDisabled}/>;
    if (field.field_type === "boolean") return <DesignSystemCheckbox key={field.key} label={field.label} checked={Boolean(value)} onChange={next => setValue(field.key, next)} isDisabled={isDisabled}/>;
    if (field.field_type === "select") return <Selector key={field.key} label={label} value={value} onChange={next => setValue(field.key, next)} options={[{value: "", label: t("common.selectPlaceholder")}, ...(field.options || []).map(option => ({value: option, label: option}))]} isDisabled={isDisabled} description={field.help_text}/>;
    if (field.field_type === "date") return <label className="metadata-native-field" key={field.key}><span>{label}</span><input type="date" value={value} onChange={event => setValue(field.key, event.target.value)} disabled={isDisabled}/>{field.help_text && <small>{field.help_text}</small>}</label>;
    return <TextInput key={field.key} label={label} value={String(value)} onChange={next => setValue(field.key, field.field_type === "number" && next !== "" ? Number(next) : next)} type={field.field_type === "number" ? "number" : "text"} description={field.help_text} isDisabled={isDisabled}/>;
  })}</div>;
}

function DocumentTypeEditor({draft, setDraft, editing, error, setError, onSubmit, onCancel, profileDefaults}) {
  const {t} = useLanguage();
  const addField = () => setDraft(current => ({...current, fields: [...current.fields, {key: "", label: "", field_type: "text", required: false, help_text: "", options: [], searchable: true, filterable: false, graph_entity_type: "", graph_relationship: ""}]}));
  const copyProfileDefaults = () => setDraft(current => ({...current, fields: (profileDefaults[current.base_document_type] || []).map(field => ({...field}))}));
  const updateField = (index, patch) => setDraft(current => ({...current, fields: current.fields.map((field, currentIndex) => currentIndex === index ? {...field, ...patch} : field)}));
  const removeField = index => setDraft(current => ({...current, fields: current.fields.filter((_, currentIndex) => currentIndex !== index)}));
  return <form className="template-form" onSubmit={onSubmit}>
    <div className="drawer-form-heading"><div><p className="eyebrow">{editing ? t("documentType.editor.editEyebrow") : t("documentType.editor.newEyebrow")}</p><h3>{editing ? t("documentType.editor.editTitle") : t("documentType.editor.createTitle")}</h3></div><span className="section-copy">{t("documentType.editor.description")}</span></div>
    <TextInput label={t("documentType.editor.typeName")} value={draft.name} onChange={name => setDraft(current => ({...current, name}))} placeholder={t("documentType.editor.typeNamePlaceholder")} isRequired/>
    <TextInput label={t("documentType.editor.shortDescription")} value={draft.description} onChange={description => setDraft(current => ({...current, description}))} placeholder={t("documentType.editor.shortDescriptionPlaceholder")} isOptional optionalLabel={t("common.optional")}/>
    <Selector label={t("documentType.editor.processingProfile")} value={draft.base_document_type} onChange={base_document_type => setDraft(current => ({...current, base_document_type, fields: current.fields.length ? current.fields : (profileDefaults[base_document_type] || []).map(field => ({...field}))}))} options={DOCUMENT_TYPE_OPTIONS.map(option => ({value: option.value, label: t(option.labelKey)}))}/>
    <div className="template-field-builder"><div><b>{t("documentType.editor.metadataFields")}</b><span className="template-field-actions"><Button label={t("documentType.editor.useProfileDefaults")} type="button" size="sm" variant="ghost" onClick={copyProfileDefaults} isDisabled={!profileDefaults[draft.base_document_type]?.length}/><Button label={t("documentType.editor.addField")} type="button" size="sm" variant="ghost" onClick={addField}/></span></div>{draft.fields.map((field, index) => <div className="template-field-row" key={`${field.key}-${index}`}>
      <div className="template-field-control"><TextInput label={t("documentType.editor.fieldKey")} value={field.key} onChange={key => updateField(index, {key})} placeholder="issuer"/></div>
      <div className="template-field-control"><TextInput label={t("documentType.editor.label")} value={field.label} onChange={label => updateField(index, {label})} placeholder={t("documentType.editor.labelPlaceholder")}/></div>
      <div className="template-field-control"><Selector label={t("documentType.editor.fieldType")} value={field.field_type} onChange={field_type => updateField(index, {field_type})} options={["text", "textarea", "date", "number", "select", "boolean"].map(value => ({value, label: value}))}/></div>
      <div className="template-field-control template-field-required"><DesignSystemCheckbox label={t("documentType.editor.required")} checked={field.required} onChange={required => updateField(index, {required})}/></div>
      <div className="template-field-control template-field-help"><TextInput label={t("documentType.editor.helpText")} value={field.help_text || ""} onChange={help_text => updateField(index, {help_text})} placeholder={t("documentType.editor.helpTextPlaceholder")} isOptional optionalLabel={t("common.optional")}/></div>
      {field.field_type === "select" && <div className="template-field-control template-field-options"><TextInput label={t("documentType.editor.options")} value={(field.options || []).join(", ")} onChange={value => updateField(index, {options: value.split(",").map(item => item.trim()).filter(Boolean)})} placeholder={t("documentType.editor.optionsPlaceholder")}/></div>}
      <details className="template-field-advanced"><summary>{t("documentType.editor.capabilitiesSummary")}</summary><div className="template-field-capabilities"><DesignSystemCheckbox label={t("documentType.editor.searchCapability")} checked={field.searchable !== false} onChange={searchable => updateField(index, {searchable})}/><DesignSystemCheckbox label={t("documentType.editor.filterCapability")} checked={Boolean(field.filterable)} onChange={filterable => updateField(index, {filterable})}/><DesignSystemCheckbox label={t("documentType.editor.graphCapability")} checked={Boolean(field.graph_relationship)} onChange={enabled => updateField(index, enabled ? {graph_entity_type: field.graph_entity_type || "Entity", graph_relationship: field.graph_relationship || "RELATED_TO"} : {graph_entity_type: "", graph_relationship: ""})}/></div>{field.graph_relationship && <div className="template-field-control template-field-graph"><TextInput label={t("documentType.editor.graphEntityType")} value={field.graph_entity_type || ""} onChange={graph_entity_type => updateField(index, {graph_entity_type})} placeholder={t("documentType.editor.graphEntityTypePlaceholder")}/><TextInput label={t("documentType.editor.relationship")} value={field.graph_relationship || ""} onChange={graph_relationship => updateField(index, {graph_relationship: graph_relationship.toUpperCase().replace(/[^A-Z0-9_]/g, "")})} placeholder="ISSUED_BY"/></div>}</details>
      <div className="template-field-action"><Button label={t("documentType.editor.remove")} type="button" size="sm" variant="destructive" onClick={() => removeField(index)}/></div>
    </div>)}</div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    <div className="preview-actions"><Button label={editing ? t("documentType.editor.save") : t("documentType.editor.createTitle")} type="submit" variant="primary"/><Button label={t("common.cancel")} type="button" variant="ghost" onClick={onCancel}/></div>
  </form>;
}

function DocumentTypeDrawer({open, templates, onClose, onCreate, onUpdate, onDeactivate, onActivate}) {
  const {t} = useLanguage();
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
    if (!draft.name.trim()) { setError(t("documentType.drawer.error.nameRequired")); return; }
    if (draft.fields.some(field => !/^[a-z][a-z0-9_]*$/.test(field.key) || !field.label.trim())) { setError(t("documentType.drawer.error.fieldInvalid")); return; }
    if (new Set(draft.fields.map(field => field.key)).size !== draft.fields.length) { setError(t("documentType.drawer.error.duplicateKey")); return; }
    try { if (editing) await onUpdate(editing.id, draft); else await onCreate(draft); resetEditor(); }
    catch (requestError) { setError(requestError.message || t("documentType.drawer.error.saveFailed")); }
  };
  const renderRow = template => <article className="document-type-row" key={template.id}>
    <div className="document-type-row-main"><div className="document-type-row-title"><b>{template.name}</b><span className={`template-status ${template.is_active === false ? "inactive" : "active"}`}>{template.is_active === false ? t("common.inactive") : t("common.active")}</span>{template.is_system && <span className="template-system-badge">{t("documentType.drawer.builtInBadge")}</span>}</div><p>{template.description || t("documentType.drawer.noDescription")}</p><small>{template.base_document_type} · {t(template.fields.length === 1 ? "documentType.drawer.fieldCountOne" : "documentType.drawer.fieldCountOther", {count: template.fields.length})} · {t(template.usage_count === 1 ? "documentType.drawer.usageCountOne" : "documentType.drawer.usageCountOther", {count: template.usage_count || 0})} · v{template.version}</small></div>
    {!template.is_system && <div className="document-type-row-actions"><Button label={t("common.edit")} size="sm" variant="ghost" onClick={() => startEdit(template)}/>{template.is_active === false ? <Button label={t("common.restore")} size="sm" variant="secondary" onClick={() => onActivate(template)}/> : <Button label={t("documentType.drawer.archive")} size="sm" variant="ghost" onClick={() => onDeactivate(template)}/>}</div>}
  </article>;
  return <div className="document-type-drawer-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><aside ref={drawerRef} className="document-type-drawer" role="dialog" aria-modal="true" aria-labelledby="document-type-drawer-title" onMouseDown={event => event.stopPropagation()}>
    <header className="document-type-drawer-header"><div><p className="eyebrow">{t("documentType.drawer.eyebrow")}</p><h2 id="document-type-drawer-title" tabIndex={-1} ref={headingRef}>{t("documentType.drawer.title")}</h2><p>{t("documentType.drawer.countInKb", {count: templates.length})}</p></div><button type="button" className="drawer-close" onClick={onClose} aria-label={t("documentType.drawer.close")}>×</button></header>
    <div className="document-type-controls"><TextInput label={t("documentType.drawer.findType")} value={search} onChange={setSearch} placeholder={t("documentType.drawer.findTypePlaceholder")}/><Selector label={t("common.status")} value={statusFilter} onChange={setStatusFilter} options={[{value: "all", label: t("common.allStatuses")}, {value: "active", label: t("common.active")}, {value: "inactive", label: t("common.inactive")}]}/></div>
    <div className="document-type-drawer-actions"><Button label={creating || editing ? t("documentType.drawer.cancelEditing") : t("documentType.drawer.createType")} size="sm" variant="primary" onClick={() => (creating || editing) ? resetEditor() : startCreate()}/></div>
    {(creating || editing) && <DocumentTypeEditor draft={draft} setDraft={setDraft} editing={editing} error={error} setError={setError} onSubmit={submit} onCancel={resetEditor} profileDefaults={profileDefaults}/>}
    <section className="document-type-section"><div className="document-type-section-heading"><h3>{t("documentType.drawer.builtInTypes")}</h3><span>{systemTemplates.length}</span></div>{systemTemplates.length ? systemTemplates.map(renderRow) : <p className="document-type-empty">{t("documentType.drawer.noBuiltInMatch")}</p>}</section>
    <section className="document-type-section"><div className="document-type-section-heading"><h3>{t("documentType.drawer.customTypes")}</h3><span>{customTemplates.length}</span></div>{customTemplates.length ? customTemplates.map(renderRow) : <p className="document-type-empty">{t("documentType.drawer.noCustomTypes")}</p>}</section>
  </aside></div>;
}

function Documents({selectedKb, documents, documentTotal, documentOffset, setDocumentOffset, documentSearch, setDocumentSearch, documentStatusFilter, setDocumentStatusFilter, documentTypeFilter, setDocumentTypeFilter, documentsLoading, hasCompletedDocuments, showDeletedDocuments, setShowDeletedDocuments, uploadFile, setUploadFile, uploadTitle, setUploadTitle, uploadDocumentType, setUploadDocumentType, documentTemplates, uploadTemplateId, setUploadTemplateId, uploadMetadata, setUploadMetadata, createDocumentTemplate, updateDocumentTemplate, deactivateDocumentTemplate, activateDocumentTemplate, uploadDocument, isUploading, openDocument, extractLegalMetadata, saveLegalMetadata, deleteLegalMetadata, saveDocumentMetadata, reprocessDocument, deleteDocument, restoreDocument, reindexEmbeddings, refreshDocuments, documentPreview, documentJobs, documentJobPolling, documentJobPollError, legalInstruments, resolveLegalRegistry, updateLegalInstrument, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship, onClosePreview, onCreateKb, onSearch, onExplore}) {
  const {t} = useLanguage();
  const [isTypeDrawerOpen, setIsTypeDrawerOpen] = useState(false);
  const [isUploadDrawerOpen, setIsUploadDrawerOpen] = useState(false);
  const [uploadStep, setUploadStep] = useState(1);
  const [libraryTab, setLibraryTab] = useState("files");
  const typeManagerTriggerRef = useRef(null);
  const uploadTriggerRef = useRef(null);
  const uploadDrawerRef = useRef(null);
  const uploadDrawerHeadingRef = useRef(null);
  const documentTriggerRef = useRef(null);
  const closeTypeDrawer = useCallback(() => {
    setIsTypeDrawerOpen(false);
    window.requestAnimationFrame(() => typeManagerTriggerRef.current?.focus());
  }, []);
  const closeUploadDrawer = useCallback(() => {
    setIsUploadDrawerOpen(false);
    setUploadStep(1);
    window.requestAnimationFrame(() => uploadTriggerRef.current?.focus());
  }, []);
  const openDocumentFromLibrary = (document, event) => {
    documentTriggerRef.current = event?.currentTarget || null;
    openDocument(document);
  };
  const closeDocumentPreview = useCallback(() => {
    onClosePreview();
    window.requestAnimationFrame(() => documentTriggerRef.current?.focus());
  }, [onClosePreview]);
  useEffect(() => { setLibraryTab("files"); }, [selectedKb?.id]);
  useEffect(() => {
    if (!uploadFile.length && uploadStep !== 1) setUploadStep(1);
  }, [uploadFile.length, uploadStep]);
  useDialogFocus({open: isUploadDrawerOpen, dialogRef: uploadDrawerRef, initialFocusRef: uploadDrawerHeadingRef, onClose: closeUploadDrawer});
  if (!selectedKb) return <EmptyState title={t("documents.emptyKb.title")} description={t("documents.emptyKb.description")} actions={<Button label={t("documents.emptyKb.action")} variant="primary" onClick={onCreateKb}/>}/>;
  const pageSize = 50;
  const pageStart = documentTotal ? documentOffset + 1 : 0;
  const pageEnd = Math.min(documentOffset + documents.length, documentTotal);
  const hasPrevious = documentOffset > 0;
  const hasNext = documentOffset + documents.length < documentTotal;
  const processingStatus = document => document.processing_job_status && ["queued", "running"].includes(document.processing_job_status) ? document.processing_job_status : document.status;
  const activeTemplates = documentTemplates.filter(template => template.is_active !== false);
  const uploadTemplate = activeTemplates.find(template => template.id === uploadTemplateId) || activeTemplates[0] || {id: "system:general", name: t("documents.upload.fallbackTemplateName"), base_document_type: uploadDocumentType, fields: [], description: t("documents.upload.fallbackTemplateDescription")};
  const templateLabel = template => template.is_system ? documentTypeLabel(t, template.base_document_type) : template.name;
  const templateDescription = template => template.is_system ? documentTypeDescription(t, template.base_document_type) : template.description || t("documents.upload.fallbackTemplateDescription");
  const uploadSteps = [[1, "documents.upload.step.files"], [2, "documents.upload.step.details"], [3, "documents.upload.step.review"]];
  const handleUploadSubmit = async event => {
    if (uploadStep < 3) {
      event.preventDefault();
      if (uploadStep === 1 && !uploadFile.length) return;
      setUploadStep(step => Math.min(3, step + 1));
      return;
    }
    const uploaded = await uploadDocument(event);
    if (uploaded) closeUploadDrawer();
  };
  const uploadStepper = <nav className="upload-stepper" aria-label={t("documents.upload.stepperLabel")}>
    <ol>{uploadSteps.map(([step, labelKey]) => <li key={step} className={uploadStep === step ? "is-active" : uploadStep > step ? "is-complete" : ""} aria-current={uploadStep === step ? "step" : undefined}><span className="upload-step-number" aria-hidden="true">{uploadStep > step ? "✓" : step}</span><span>{t(labelKey)}</span></li>)}</ol>
  </nav>;
  const uploadForm = <form className="upload-layout" onSubmit={handleUploadSubmit}>
    {uploadStep === 1 && <FileInput label={t("documents.upload.addDocuments")} value={uploadFile} onChange={files => setUploadFile(Array.isArray(files) ? files : files ? [files] : [])} onRemove={index => setUploadFile(current => current.filter((_, fileIndex) => fileIndex !== index))} removeLabel={t("ui.file.remove")} isMultiple maxFiles={20} accept={ACCEPTED_FILES} maxSize={MAX_FILE_SIZE} mode="dropzone" description={t("documents.upload.description", {maxSize: MAX_FILE_SIZE_MB})} isLoading={isUploading} chooseLabel={t("ui.file.choose")} uploadingLabel={t("ui.file.uploading")} tooManyFilesMessage={t("ui.file.tooMany", {maxFiles: 20})} tooLargeFilesMessage={files => t("ui.file.tooLarge", {maxSize: MAX_FILE_SIZE_MB, names: files.map(file => file.name).join(", ")})}/>}
    {uploadStep === 2 && uploadFile.length > 0 && <div className="upload-meta">
      <div className="upload-selection-summary" role="status">{t(uploadFile.length === 1 ? "documents.upload.fileSummaryOne" : "documents.upload.fileSummaryOther", {count: uploadFile.length})}</div>
      <Selector label={t("documents.upload.documentType")} value={uploadTemplate.id} onChange={templateId => { const next = activeTemplates.find(template => template.id === templateId); setUploadTemplateId(templateId); setUploadDocumentType(next?.base_document_type || "general"); setUploadMetadata({}); }} options={activeTemplates.map(template => ({value: template.id, label: templateLabel(template)}))} isDisabled={isUploading} size="md"/>
      <p className="section-copy document-type-help">{t("documents.upload.templateHelp", {description: templateDescription(uploadTemplate)})}</p>
      <MetadataFields fields={uploadTemplate.fields} values={uploadMetadata} onChange={setUploadMetadata} isDisabled={isUploading}/>
      <TextInput label={t("documents.upload.titleLabel")} value={uploadTitle} onChange={setUploadTitle} placeholder={t("documents.upload.titlePlaceholder")} isOptional optionalLabel={t("common.optional")} isDisabled={uploadFile.length !== 1 || isUploading}/>
      {uploadFile.length > 1 && <p className="section-copy document-type-help">{t("documents.upload.batchNote")}</p>}
    </div>}
    {uploadStep === 3 && <section className="upload-review" aria-labelledby="upload-review-title"><h3 id="upload-review-title">{t("documents.upload.reviewTitle")}</h3><p>{t("documents.upload.reviewDescription")}</p><dl><div><dt>{t("documents.upload.documentType")}</dt><dd>{templateLabel(uploadTemplate)}</dd></div><div><dt>{t(uploadFile.length === 1 ? "documents.upload.fileSummaryOne" : "documents.upload.fileSummaryOther", {count: uploadFile.length})}</dt><dd>{uploadFile.map(file => file.name).join(", ")}</dd></div></dl></section>}
    <div className="upload-actions">{uploadStep > 1 && <Button label={t("documents.upload.back")} type="button" variant="ghost" onClick={() => setUploadStep(step => Math.max(1, step - 1))} isDisabled={isUploading}/>}<Button label={uploadStep < 3 ? t("documents.upload.next") : uploadFile.length > 1 ? t("documents.upload.submitMultiple", {count: uploadFile.length}) : t("documents.upload.submitSingle")} type="submit" variant="primary" isDisabled={uploadStep === 1 && !uploadFile.length} isLoading={uploadStep === 3 && isUploading}/></div>
  </form>;
  return <><PageHeading eyebrow={t("documents.pageHeading.eyebrow")} title={t("documents.pageHeading.title", {name: selectedKb.name})} description={t("documents.pageHeading.description")} actions={<><Button ref={uploadTriggerRef} label={t("documents.upload.addDocuments")} variant="primary" onClick={() => { setUploadStep(1); setIsUploadDrawerOpen(true); }}/><Button ref={typeManagerTriggerRef} label={t("documents.manageTypes")} variant="secondary" onClick={() => setIsTypeDrawerOpen(true)}/><Button label={showDeletedDocuments ? t("documents.hideDeleted") : t("documents.showDeleted")} variant="ghost" onClick={() => { setDocumentOffset(0); setShowDeletedDocuments(value => !value); }}/><Button label={t("documents.reindex")} variant="secondary" onClick={reindexEmbeddings}/><Button label={t("documents.refreshStatus")} variant="ghost" onClick={refreshDocuments}/></>}/>
    {isUploadDrawerOpen && <div className="document-type-drawer-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) closeUploadDrawer(); }}><aside ref={uploadDrawerRef} className="document-type-drawer upload-drawer" role="dialog" aria-modal="true" aria-labelledby="upload-drawer-title" onMouseDown={event => event.stopPropagation()}><header className="document-type-drawer-header"><div><p className="eyebrow">{t("documents.pageHeading.eyebrow")}</p><h2 id="upload-drawer-title" tabIndex={-1} ref={uploadDrawerHeadingRef}>{t("documents.upload.addDocuments")}</h2><p>{t("documents.upload.formatNote")}</p></div><button type="button" className="drawer-close" onClick={closeUploadDrawer} aria-label={t("documentPreview.closeAriaLabel")}>×</button></header>{uploadStepper}{uploadForm}</aside></div>}
    {legalInstruments?.length > 0 && <div className="log-tabs" role="tablist"><button role="tab" aria-selected={libraryTab === "files"} className={libraryTab === "files" ? "selected" : ""} onClick={() => setLibraryTab("files")}>{t("documents.tabs.files")}</button><button role="tab" aria-selected={libraryTab === "legal"} className={libraryTab === "legal" ? "selected" : ""} onClick={() => setLibraryTab("legal")}>{t("documents.tabs.legal")}</button></div>}
    {(libraryTab === "files" || !(legalInstruments?.length > 0)) && <section className="content-section"><div className="section-title"><div><h2>{showDeletedDocuments ? t("documents.library.allTitle") : t("documents.library.title")}</h2><p>{documentTotal ? t(documentTotal === 1 ? "documents.library.showingCountOne" : "documents.library.showingCountOther", {start: pageStart, end: pageEnd, total: documentTotal}) : t("documents.library.empty")}</p></div>{documents.some(document => ["queued", "extracting", "indexing"].includes(document.status) || ["queued", "running"].includes(document.processing_job_status)) && <span className="live-status" role="status">{t("documents.library.updatingLive")}</span>}</div>
      <div className="document-filter-bar"><TextInput label={t("documents.filter.findLabel")} value={documentSearch} onChange={value => { setDocumentOffset(0); setDocumentSearch(value); }} placeholder={t("documents.filter.findPlaceholder")}/><Selector label={t("common.status")} value={documentStatusFilter} onChange={value => { setDocumentOffset(0); setDocumentStatusFilter(value); }} options={[{value: "all", label: t("common.allStatuses")}, ...STATUS_KEYS.filter(key => key !== "disabled").map(key => ({value: key, label: t(`status.${key}.label`)})), {value: "deleted", label: t("documents.filter.status.deleted")}]}/><Selector label={t("documents.upload.documentType")} value={documentTypeFilter} onChange={value => { setDocumentOffset(0); setDocumentTypeFilter(value); }} options={[{value: "all", label: t("common.allTypes")}, ...DOCUMENT_TYPE_OPTIONS.map(option => ({value: option.value, label: t(option.labelKey)})), ...documentTemplates.filter(template => !template.is_system).map(template => ({value: template.id, label: template.name}))]}/></div>
    {documentsLoading && !documents.length ? <p className="section-copy" role="status">{t("documents.loading")}</p> : documents.length ? <div className="document-table">{documents.map(document => { const activeStatus = processingStatus(document); const processing = ["queued", "extracting", "indexing"].includes(document.status) || ["queued", "running"].includes(document.processing_job_status); const failed = ["failed", "ocr_required"].includes(document.status) || document.processing_job_status === "failed"; return <article key={document.id} className="document-item"><div className="document-main"><button type="button" className="document-title" onClick={event => openDocumentFromLibrary(document, event)}>{document.title || document.original_filename}</button><p>{document.original_filename} · {Math.ceil(document.file_size / 1024)} KB · {document.metadata_template_name || documentTypeLabel(t, document.document_type)}</p>{processing && <><ProgressBar label={`${document.title || document.original_filename} processing`} value={document.processing_job_progress_percent ?? 0} variant="warning" isIndeterminate={document.processing_job_progress_percent == null}/><p className="document-status-help">{statusHelp(t, activeStatus) || document.processing_job_stage || t("documents.status.processing")}</p></>}{failed && <p className="document-status-help document-status-warning">{statusHelp(t, document.status) || t("status.failed.help")}{document.error_code ? ` (${document.error_code})` : ""}</p>}</div><StatusBadge status={document.status}/><div className="document-actions"><Button label={t("documents.action.openDetails")} variant="ghost" size="sm" onClick={event => openDocumentFromLibrary(document, event)}/>{document.deleted_at ? <Button label={t("common.restore")} variant="secondary" size="sm" onClick={() => restoreDocument(document)}/> : <><Button label={t("documents.action.processAgain")} variant="secondary" size="sm" isDisabled={processing} onClick={() => reprocessDocument(document)}/><Button label={t("common.delete")} variant="destructive" size="sm" onClick={() => deleteDocument(document)}/></>}</div></article>; })}</div> : <EmptyState title={documentTotal ? t("documents.empty.noMatch.title") : t("documents.empty.readyTitle")} description={documentTotal ? t("documents.empty.noMatch.description") : t("documents.empty.readyDescription")}/>}
      {documentTotal > pageSize && <div className="document-pagination"><Button label={t("common.previous")} variant="ghost" size="sm" isDisabled={!hasPrevious || documentsLoading} onClick={() => setDocumentOffset(Math.max(0, documentOffset - pageSize))}/><span>{pageStart}–{pageEnd} / {documentTotal}</span><Button label={t("common.next")} variant="secondary" size="sm" isDisabled={!hasNext || documentsLoading} onClick={() => setDocumentOffset(documentOffset + pageSize)}/></div>}
    </section>}
    {hasCompletedDocuments && <section className="next-step-card"><div><p className="eyebrow">{t("documents.nextStep.eyebrow")}</p><h2>{t("documents.nextStep.title")}</h2><p>{t("documents.nextStep.description")}</p></div><div className="next-step-actions"><Button label={t("documents.nextStep.search")} variant="primary" onClick={onSearch}/><Button label={t("workflow.explore")} variant="secondary" onClick={onExplore}/></div></section>}
    {libraryTab === "legal" && legalInstruments?.length > 0 && <LegalInstrumentsTab knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship} resolveLegalRegistry={resolveLegalRegistry} onOpenDocument={openDocumentFromLibrary}/>}
    {documentPreview && <DocumentPreview preview={documentPreview} jobs={documentJobs} isPollingJobs={documentJobPolling} pollingError={documentJobPollError} templates={documentTemplates} legalInstrument={legalInstruments?.find(row => row.document_id === documentPreview.document_id)} onExtractLegal={extractLegalMetadata} onSaveLegal={saveLegalMetadata} onDeleteLegal={deleteLegalMetadata} onSaveDocumentMetadata={saveDocumentMetadata} onUpdateLegalInstrument={updateLegalInstrument} onClose={closeDocumentPreview}/>}<DocumentTypeDrawer open={isTypeDrawerOpen} templates={documentTemplates} onClose={closeTypeDrawer} onCreate={createDocumentTemplate} onUpdate={updateDocumentTemplate} onDeactivate={deactivateDocumentTemplate} onActivate={activateDocumentTemplate}/></>
}

const legalStatusLabel = (labels, status) => labels.status[status] || labels.status.unknown;
const legalClassLabel = (labels, instrument) => labels.class[instrument.document_class] || labels.kind[instrument.kind] || labels.entity.LegalInstrument;
const legalEntityLabel = (labels, type) => labels.entity[type] || type || labels.entityFallback;
const relationshipLabel = (labels, type) => labels.relationship[type] || String(type || "").replace(/_/g, " ");
const reviewStatusLabel = (labels, status) => labels.reviewStatus[status] || labels.reviewStatus.unreviewed;
const relationshipOriginLabel = (labels, origin) => labels.relationshipOrigin[origin] || String(origin || labels.relationshipOriginFallback).replace(/_/g, " ");
const reviewBadgeVariant = status => ({verified: "success", suggested: "warning", rejected: "error"}[status] || "neutral");
const legalDateValue = instrument => instrument.effective_from || instrument.version_date || "";
const legalDateLabel = (t, instrument) => {
  if (!instrument.effective_from) return instrument.version_date ? t("legal.date.versionDated", {date: instrument.version_date}) : t("legal.date.undated");
  return instrument.effective_to ? t("legal.date.effectiveRange", {from: instrument.effective_from, to: instrument.effective_to}) : t("legal.date.effectiveFrom", {date: instrument.effective_from});
};


function LegalInstrumentOverrideForm({row, onSave}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
  const [status, setStatus] = useState(row.status);
  const [effectiveFrom, setEffectiveFrom] = useState(row.effective_from || "");
  const [effectiveTo, setEffectiveTo] = useState(row.effective_to || "");
  const [sourceUri, setSourceUri] = useState(row.source_uri || "");
  const [sourceReference, setSourceReference] = useState(row.source_reference || "");
  const submit = event => { event.preventDefault(); onSave({status, effective_from: effectiveFrom || null, effective_to: effectiveTo || null, source_uri: sourceUri || null, source_reference: sourceReference || null}); };
  return <form className="legal-override-form" onSubmit={submit}><label className="native-field">{t("common.status")}<select value={status} onChange={event => setStatus(event.target.value)}>{Object.keys(labels.status).map(value => <option key={value} value={value}>{labels.status[value]}</option>)}</select></label><label className="native-field">{t("legal.override.effectiveFrom")}<input type="date" value={effectiveFrom} onChange={event => setEffectiveFrom(event.target.value)}/></label><label className="native-field">{t("legal.override.effectiveTo")}<input type="date" value={effectiveTo} onChange={event => setEffectiveTo(event.target.value)}/></label><label className="native-field">{t("legal.override.sourceUri")}<input value={sourceUri} onChange={event => setSourceUri(event.target.value)} placeholder={t("legal.override.sourceUriPlaceholder")}/></label><label className="native-field">{t("legal.override.sourceReference")}<input value={sourceReference} onChange={event => setSourceReference(event.target.value)} placeholder={t("legal.override.sourceReferencePlaceholder")}/></label><Button label={t("legal.override.save")} type="submit" size="sm" variant="primary"/><p className="section-copy">{t("legal.override.help")}</p></form>;
}

function SearchView({selectedKb, documents, completedDocuments, query, setQuery, queryAsOfDate, setQueryAsOfDate, queryIncludeHistorical, setQueryIncludeHistorical, runQuery, isQuerying, queryResult, submitFeedback, onDocuments, onOpenSource}) {
  const {t} = useLanguage();
  if (!selectedKb) return <EmptyState title={t("search.empty.noKb.title")} description={t("search.empty.noKb.description")} actions={<Button label={t("search.empty.noKb.action")} variant="primary" onClick={onDocuments}/>}/>;
  if (!completedDocuments) return <EmptyState title={t("search.empty.noDocs.title")} description={documents.length ? t("search.empty.noDocs.description.processing") : t("search.empty.noDocs.description.none")} actions={<Button label={documents.length ? t("search.empty.noDocs.action.processing") : t("search.empty.noDocs.action.none")} variant="primary" onClick={onDocuments}/>}/>;
  const examples = [t("search.example.dependencies"), t("search.example.architecture"), t("search.example.impact")];
  return <><PageHeading eyebrow={t("search.pageHeading.eyebrow")} title={t("search.pageHeading.title")} description={t("search.pageHeading.description", {name: selectedKb.name})}/><Card padding={4} variant="blue"><form className="search-form" onSubmit={runQuery}><TextArea label={t("search.form.questionLabel")} value={query} onChange={setQuery} rows={4} placeholder={t("search.form.questionPlaceholder")} isRequired/><div className="example-row"><span>{t("search.form.tryExample")}</span>{examples.map(example => <button key={example} type="button" className="example-chip" onClick={() => setQuery(example)}>{example}</button>)}</div>
    <details className="legal-query-filters"><summary>{t("search.filters.summary")}</summary><div className="legal-query-filters-row"><label className="native-field">{t("search.filters.asOfDate")}<input type="date" value={queryAsOfDate} onChange={event => setQueryAsOfDate(event.target.value)}/></label><label><input type="checkbox" checked={queryIncludeHistorical} onChange={event => setQueryIncludeHistorical(event.target.checked)}/> {t("search.filters.includeHistorical")}</label></div><p className="section-copy">{t("search.filters.help")}</p></details>
    <Button label={isQuerying ? t("search.form.searching") : t("search.form.submit")} type="submit" variant="primary" size="lg" isDisabled={!query.trim() || isQuerying} isLoading={isQuerying}/>{isQuerying && <p className="query-progress" role="status" aria-live="polite">{t("search.progress")}</p>}</form></Card>{queryResult && <QueryResult data={queryResult} submitFeedback={submitFeedback} onOpenSource={onOpenSource}/>}</>;
}

function ExploreView({selectedKb, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship, resolveLegalRegistry, onOpenDocument}) {
  const {t} = useLanguage();
  if (!selectedKb) return <EmptyState title={t("explore.empty.noKb.title")} description={t("explore.empty.noKb.description")}/>;
  const description = isLegalGraph ? t("explore.pageHeading.description.legal") : t("explore.pageHeading.description.general");
  return <><PageHeading eyebrow={t("explore.pageHeading.eyebrow")} title={isLegalGraph ? t("explore.pageHeading.title.legal") : t("explore.pageHeading.title.general")} description={description}/>
    <GraphWorkspace knowledgeBaseId={selectedKb.id} entities={entities} relationships={relationships} addEntity={addEntity} addRelationship={addRelationship} impact={impact} analyzeImpact={analyzeImpact} syncGraphFromDocuments={syncGraphFromDocuments} refreshGraph={refreshGraph} isLegalGraph={isLegalGraph} legalGraphView={legalGraphView} setLegalGraphView={setLegalGraphView} queueLegalGraphRebuild={queueLegalGraphRebuild} legalRebuildStatus={legalRebuildStatus} reviewLegalRelationship={reviewLegalRelationship} resolveLegalRegistry={resolveLegalRegistry} onOpenDocument={onOpenDocument}/>
  </>;
}

const GRAPH_TYPE_VISUALS = {
  application: {Icon: AppWindow, color: "#2786C2", tint: "#eaf4fa"},
  service: {Icon: Cloud, color: "#5f5fc4", tint: "#efeffb"},
  server: {Icon: HardDrives, color: "#0d7c83", tint: "#e8f7f7"},
  database: {Icon: Database, color: "#287d65", tint: "#e8f6ef"},
  businessprocess: {Icon: GitBranch, color: "#c27a18", tint: "#fff5e5"},
  organization: {Icon: Buildings, color: "#a14878", tint: "#fbeef5"},
  person: {Icon: User, color: "#9a5a20", tint: "#fff3e7"},
  document: {Icon: FileText, color: "#6f6a63", tint: "#f4f2ef"},
  concept: {Icon: Lightbulb, color: "#7454bf", tint: "#f2effc"},
  legalinstrument: {Icon: Gavel, color: "#6750a4", tint: "#f1edfb"},
  provision: {Icon: BookOpen, color: "#2786C2", tint: "#eaf4fa"},
  legalauthority: {Icon: Buildings, color: "#0d7c83", tint: "#e8f7f7"},
  legalparty: {Icon: User, color: "#a14878", tint: "#fbeef5"},
  obligation: {Icon: Scales, color: "#c27a18", tint: "#fff5e5"},
  right: {Icon: ShieldCheck, color: "#287d65", tint: "#e8f6ef"},
  prohibition: {Icon: Gavel, color: "#a04646", tint: "#fceded"},
  penalty: {Icon: Scales, color: "#9a5a20", tint: "#fff3e7"},
  definition: {Icon: BookOpen, color: "#54717b", tint: "#edf3f5"},
  amendment: {Icon: FileText, color: "#5f5fc4", tint: "#efeffb"},
  default: {Icon: CirclesThree, color: "#54717b", tint: "#edf3f5"},
};
const graphTypeKey = type => String(type || "").replace(/[^a-z]/gi, "").toLowerCase();
const graphTypeVisual = type => GRAPH_TYPE_VISUALS[graphTypeKey(type)] || GRAPH_TYPE_VISUALS.default;
const entityTypeOrder = type => ["organization", "businessprocess", "application", "service", "server", "database", "document", "concept"].indexOf(graphTypeKey(type));
const ENTITY_TYPES = ["Application", "Service", "Server", "Database", "BusinessProcess", "Organization", "Concept"];
const LEGAL_ENTITY_TYPES = ["LegalInstrument", "Provision", "LegalAuthority", "LegalParty", "Obligation", "Right", "Prohibition", "Penalty", "Definition", "Amendment"];
const RELATIONSHIP_TYPES = ["DEPENDS_ON", "RUNS_ON", "USES", "SUPPORTS", "AFFECTS"];
const entityTypeLabel = (t, type) => { const key = `graph.entityType.${type}`; const label = t(key); return label === key ? type : label; };

function KnowledgeNode({data, selected}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
  const isLegal = Boolean(data.isLegal || labels.entity[data.entityType]);
  const reviewStatus = data.reviewStatus || "unreviewed";
  const visual = graphTypeVisual(data.entityType);
  const Icon = visual.Icon;
  const handles = [
    ["top", Position.Top],
    ["right", Position.Right],
    ["bottom", Position.Bottom],
    ["left", Position.Left],
  ];
  return <div className={`knowledge-node graph-visual-node ${selected ? "selected" : ""} ${data.isMuted ? "is-muted" : ""} ${data.isConnected ? "is-connected" : ""}`} style={{"--graph-node-color": visual.color, "--graph-node-tint": visual.tint}}>
    {handles.map(([id, position]) => <Handle key={id} id={id} type="source" position={position} className={`graph-handle graph-handle-${id}`}/>) }
    <div className="graph-node-circle" title={isLegal ? legalEntityLabel(labels, data.entityType) : entityTypeLabel(t, data.entityType)}><Icon size={28} weight="duotone" aria-hidden="true"/></div>
    <strong className="graph-node-label" title={data.label}>{data.label}</strong>
    <span className="graph-node-type" title={isLegal ? legalEntityLabel(labels, data.entityType) : entityTypeLabel(t, data.entityType)}>{isLegal ? legalEntityLabel(labels, data.entityType) : entityTypeLabel(t, data.entityType)}{data.documentId ? ` · ${String(data.documentId).slice(0, 8)}` : ""}</span>
    {isLegal && <span className={`graph-node-review ${reviewStatus}`}><i aria-hidden="true"/>{reviewStatusLabel(labels, reviewStatus)}</span>}
  </div>;
}

const graphNodeTypes = {knowledge: KnowledgeNode};

function LegalMapPanel({map, loading, onSelectInstrument, onOpenDocument, onOpenAdvanced, resolveLegalRegistry}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
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
  const openSourceDocument = (instrument, event) => onOpenDocument({id: instrument.document_id, title: instrument.title, original_filename: instrument.filename}, event);
  const renderCard = instrument => {
    const provenance = [instrument.authority_level != null ? t("legal.authorityLevel", {level: instrument.authority_level}) : null, instrument.status_reason, instrument.source_reference].filter(Boolean).join(" · ");
    return <div key={instrument.id} role="button" tabIndex={0} className={`legal-instrument-card ${instrument.id === currentInstrument?.id ? "is-current" : ""}`} onClick={() => onSelectInstrument(instrument.id)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelectInstrument(instrument.id); } }}>
      <div className="legal-instrument-card-top"><span className="legal-class-chip">{legalClassLabel(labels, instrument)}</span><span className={`legal-status ${instrument.status || "unknown"}`}><i aria-hidden="true"/>{legalStatusLabel(labels, instrument.status)}</span><Badge label={reviewStatusLabel(labels, instrument.review_status)} variant={reviewBadgeVariant(instrument.review_status)}/></div>
      <h3>{instrument.title}</h3>
      <p className="legal-instrument-filename">{instrument.filename || instrument.document_id}</p>
      {provenance && <p className="legal-instrument-authority">{provenance}{instrument.source_uri && " · "}{instrument.source_uri && <a className="legal-instrument-source-link" href={instrument.source_uri} target="_blank" rel="noreferrer" onClick={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()}>{t("legal.map.card.sourceLink")}</a>}</p>}
      <div className="legal-instrument-metrics"><span>{t("graph.nodesCount", {count: instrument.entity_count || 0})}</span><span>{t("graph.connectionsCount", {count: instrument.relationship_count || 0})}</span></div>
      <div className="legal-instrument-version">{legalDateLabel(t, instrument)}{instrument.version_label ? ` · ${instrument.version_label}` : ""}</div>
      <div className="legal-instrument-card-footer"><span className="legal-instrument-open">{t("legal.map.card.openStructure")}</span><button type="button" className="legal-instrument-open-doc" onClick={event => { event.stopPropagation(); openSourceDocument(instrument, event); }} onKeyDown={event => event.stopPropagation()}>{t("legal.map.card.openSource")}</button></div>
    </div>;
  };
  const timelineFamilies = (map?.families || []).map(family => ({...family, items: (family.instrument_ids || []).map(id => instrumentById[id]).filter(Boolean).sort((left, right) => String(legalDateValue(left)).localeCompare(String(legalDateValue(right))))})).filter(family => family.items.length);
  const groupLabel = key => labels.class[key] || (key === "current" ? t("legal.filter.current") : key === "historical" ? t("legal.filter.historical") : t("legal.map.group.otherFallback"));
  return <section className="legal-map-shell">
    <div className="legal-map-header">
      <div>
        <p className="eyebrow">{t("legal.map.eyebrow")}</p>
        <h2>{t("legal.map.title")}</h2>
        <p className="section-copy">{t("legal.map.description")}</p>
      </div>
      <div className="preview-actions"><Button label={t("legal.map.resolveStatuses")} size="sm" variant="secondary" onClick={resolveLegalRegistry}/><Button label={t("legal.map.advancedGraph")} size="sm" variant="secondary" onClick={onOpenAdvanced}/></div>
    </div>
    <div className="legal-map-stat-grid">
      <div className="legal-map-stat primary"><span>{t("legal.map.stat.totalLabel")}</span><strong>{instruments.length}</strong><small>{t("legal.map.stat.totalHelp")}</small></div>
      <div className="legal-map-stat"><span>{t("legal.map.stat.inForceLabel")}</span><strong>{current.length}</strong><small>{t("legal.map.stat.inForceHelp")}</small></div>
      <div className="legal-map-stat"><span>{t("legal.map.stat.historicalLabel")}</span><strong>{historical.length}</strong><small>{t("legal.map.stat.historicalHelp")}</small></div>
      <div className="legal-map-stat"><span>{t("legal.map.stat.structureLabel")}</span><strong>{nodeCount}</strong><small>{t("legal.map.stat.structureHelp", {count: relationCount})}</small></div>
    </div>
    <section className="legal-review-guide" aria-label={t("legal.map.reviewGuide.ariaLabel")}>
      <div><b>{t("legal.map.reviewGuide.title")}</b><span>{t("legal.map.reviewGuide.subtitle")}</span></div>
      <div className="legal-review-legend">
        <span><i className="verified" aria-hidden="true"/>{labels.reviewStatus.verified} <b>{relationshipSummary.verified ?? relationCount}</b></span>
        <span><i className="suggested" aria-hidden="true"/>{labels.reviewStatus.suggested} <b>{relationshipSummary.suggested ?? 0}</b></span>
        <span><i className="manual" aria-hidden="true"/>{labels.relationshipOrigin.manual} <b>{relationshipSummary.manual ?? 0}</b></span>
        <span className="legal-review-scope">{t("legal.map.reviewGuide.scope", {internal: relationshipSummary.internal ?? relationCount, cross: relationshipSummary.cross_document ?? 0})}</span>
      </div>
    </section>
    <div className="legal-map-controls">
      <label className="legal-search-field"><span>{t("legal.map.controls.searchLabel")}</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder={t("legal.map.controls.searchPlaceholder")} aria-label={t("legal.map.controls.searchAriaLabel")}/></label>
      <Selector label={t("legal.map.controls.showLabel")} value={statusFilter} onChange={setStatusFilter} options={[{value: "current", label: t("legal.filter.current")}, {value: "historical", label: t("legal.filter.historical")}, {value: "all", label: t("legal.filter.all")}]} className="legal-filter-field"/>
      <span className="legal-map-result-count">{t("legal.map.controls.resultCount", {shown: visible.length, total: instruments.length})}</span>
    </div>
    {loading && <p className="section-copy" role="status">{t("legal.map.loading")}</p>}
    {!loading && !instruments.length && <div className="legal-map-empty"><b>{t("legal.map.empty.title")}</b><span>{t("legal.map.empty.description")}</span></div>}
    {!loading && currentInstrument && <section className="legal-current-panel"><div className="legal-current-kicker"><span className="eyebrow">{t("legal.map.current.eyebrow")}</span><span className="legal-status in_force"><i aria-hidden="true"/>{t("legal.map.stat.inForceLabel")}</span><Badge label={reviewStatusLabel(labels, currentInstrument.review_status)} variant={reviewBadgeVariant(currentInstrument.review_status)}/><button type="button" className="legal-instrument-open-doc" onClick={event => openSourceDocument(currentInstrument, event)}>{t("legal.map.card.openSource")}</button></div><button type="button" className="legal-current-card" onClick={() => onSelectInstrument(currentInstrument.id)}><div><p className="legal-current-kind">{legalClassLabel(labels, currentInstrument)}</p><h3>{currentInstrument.title}</h3><p>{currentInstrument.filename || currentInstrument.document_id}</p><div className="legal-current-meta"><span><b>{legalDateLabel(t, currentInstrument)}</b></span><span>{t("graph.nodesCount", {count: currentInstrument.entity_count || 0})}</span><span>{t("graph.connectionsCount", {count: currentInstrument.relationship_count || 0})}</span>{currentInstrument.authority_level != null && <span>{t("legal.authorityLevel", {level: currentInstrument.authority_level})}</span>}</div>{currentInstrument.status_reason && <p className="legal-current-reason">{currentInstrument.status_reason}</p>}</div><span className="legal-current-action">{t("legal.map.current.viewStructure")}</span></button></section>}
    {!loading && timelineFamilies.length > 0 && <section className="legal-timeline-primary"><div className="legal-timeline-primary-heading"><div><p className="eyebrow">{t("legal.map.timeline.eyebrow")}</p><h2>{t("legal.map.timeline.title")}</h2><p className="section-copy">{t("legal.map.timeline.description")}</p></div><div className="legal-timeline-legend"><span><i className="in_force"/>{t("legal.map.stat.inForceLabel")}</span><span><i className="superseded"/>{t("legal.filter.historical")}</span></div></div>{timelineFamilies.map(family => <div className="legal-timeline-family" key={family.id}><div className="legal-timeline-family-title"><b>{family.title}</b><span>{t("legal.map.timeline.familyCount", {count: family.items.length})}</span></div><div className="legal-timeline-track legal-timeline-track-primary" style={{"--timeline-count": family.items.length}}><span className="legal-timeline-axis" aria-hidden="true"/>{family.items.map((item, index) => <button type="button" key={item.id} className={`legal-timeline-item legal-timeline-item-primary ${index % 2 === 0 ? "is-top" : "is-bottom"} ${item.id === currentInstrument?.id ? "is-current" : ""}`} onClick={() => onSelectInstrument(item.id)}><span className="legal-timeline-date">{legalDateValue(item) || t("legal.map.noDate")}</span><i className={`legal-timeline-dot ${item.status || "unknown"}`} aria-hidden="true"/><span className="legal-timeline-kind">{legalClassLabel(labels, item)}</span><strong>{item.title}</strong><small>{legalStatusLabel(labels, item.status)}{item.version_label ? ` · ${item.version_label}` : ""}</small><em>{t("legal.map.timeline.openStructure")}</em></button>)}</div></div>)}</section>}
    {!loading && visible.length > 0 && <div className="legal-instrument-sections">{Object.entries(grouped).map(([key, rows]) => <section key={key} className="legal-instrument-section"><div className="legal-instrument-section-heading"><div><p className="eyebrow">{key === "amendment" ? t("legal.map.group.amendmentsEyebrow") : key === "consolidated" ? t("legal.map.group.consolidatedEyebrow") : t("legal.map.group.historyEyebrow")}</p><h3>{groupLabel(key)}</h3></div><span>{t("legal.map.instrumentCount", {count: rows.length})}</span></div><div className="legal-instrument-grid">{rows.map(renderCard)}</div></section>)}</div>}
    {!loading && !visible.length && instruments.length > 0 && <div className="legal-map-empty"><b>{t("legal.map.noMatch.title")}</b><span>{t("legal.map.noMatch.description")}</span></div>}
  </section>;
}

function LegalGraphNavigator(props) {
  const {t} = useLanguage();
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
  if (presentation === "map") return <LegalMapPanel map={map} loading={mapLoading} onSelectInstrument={openInstrument} onOpenDocument={props.onOpenDocument} onOpenAdvanced={() => setPresentation("advanced")} resolveLegalRegistry={props.resolveLegalRegistry}/>;
  if (presentation === "instrument" && subgraph) return <section className="legal-instrument-view"><div className="legal-view-header"><div><button type="button" className="legal-back-link" onClick={() => setPresentation("map")}>{t("legal.nav.backToMap")}</button><p className="eyebrow">{t("legal.nav.documentStructure.eyebrow")}</p><h2>{subgraph.instrument?.title}</h2><p className="section-copy">{t("legal.nav.documentStructure.description")}</p></div><div className="preview-actions"><Button label={t("legal.map.advancedGraph")} size="sm" variant="secondary" onClick={() => setPresentation("advanced")}/></div></div>{subgraphLoading && <p className="section-copy" role="status">{t("legal.nav.loadingStructure")}</p>}<GraphCanvas {...props} entities={subgraph.nodes || []} relationships={subgraph.edges || []}/></section>;
  return <section className="legal-instrument-view"><div className="legal-view-header"><div><button type="button" className="legal-back-link" onClick={() => setPresentation("map")}>{t("legal.nav.backToMap")}</button><p className="eyebrow">{t("legal.nav.advanced.eyebrow")}</p><h2>{t("legal.nav.advanced.title")}</h2><p className="section-copy">{t("legal.nav.advanced.description")}</p></div><div className="preview-actions"><Button label={t("legal.nav.backToMapButton")} size="sm" variant="secondary" onClick={() => setPresentation("map")}/></div></div><GraphCanvas {...props}/></section>;
}

function GraphWorkspace(props) {
  return <ReactFlowProvider>{props.isLegalGraph ? <LegalGraphNavigator {...props}/> : <GraphCanvas {...props}/>}</ReactFlowProvider>;
}

function LegalInstrumentsTab(props) {
  return <ReactFlowProvider><LegalGraphNavigator {...props}/></ReactFlowProvider>;
}

function LegalInspector({entity, data, loading, tab, setTab, onImpact, onFocus}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
  const legal = data?.entity || entity;
  const context = data?.context || {};
  const evidence = data?.evidence || [];
  const incoming = data?.relationships?.incoming || [];
  const outgoing = data?.relationships?.outgoing || [];
  const versions = data?.versions?.family || [];
  const warnings = data?.analysis?.warnings || [];
  const statusVariant = reviewBadgeVariant(legal.review_status);
  const tabs = [["overview", t("legal.inspector.tab.overview")], ["evidence", t("legal.inspector.tab.evidence")], ["relations", t("legal.inspector.tab.relations")], ["versions", t("legal.inspector.tab.versions")]];
  return <div className="legal-inspector">
    <p className="eyebrow">{t("legal.inspector.eyebrow")}</p><h2>{legal.name}</h2>
    <div className="inspector-badges"><Badge label={legalEntityLabel(labels, legal.entity_type)} variant="info"/><Badge label={reviewStatusLabel(labels, legal.review_status)} variant={statusVariant}/>{legal.origin && <Badge label={relationshipOriginLabel(labels, legal.origin)} variant="neutral"/>}</div>
    <div className="inspector-trust-note"><b>{legal.review_status === "suggested" ? t("legal.inspector.trust.unverifiedTitle") : t("legal.inspector.trust.verifiedTitle")}</b><span>{legal.review_status === "suggested" ? t("legal.inspector.trust.unverifiedBody") : t("legal.inspector.trust.verifiedBody")}</span></div>
    <div className="inspector-tabs" role="tablist">{tabs.map(([value,label]) => <button key={value} type="button" className={tab === value ? "active" : ""} onClick={() => setTab(value)}>{label}</button>)}</div>
    {loading && <p className="section-copy" role="status">{t("legal.inspector.loading")}</p>}
    {!loading && tab === "overview" && <div className="inspector-section">
      <dl className="inspector-meta"><div><dt>{t("legal.inspector.overview.idLabel")}</dt><dd><code>{legal.identity_key || legal.id}</code></dd></div><div><dt>{t("legal.inspector.overview.confidenceLabel")}</dt><dd>{legal.confidence == null ? t("legal.inspector.overview.notSpecified") : `${Math.round(legal.confidence * 100)}%`}</dd></div><div><dt>{t("legal.inspector.overview.sourceCountLabel")}</dt><dd>{t("legal.inspector.overview.sourceCountValue", {count: legal.source_count ?? evidence.length})}</dd></div></dl>
      {context.documents?.map(document => <div className="inspector-context-card" key={document.document_id}><b>{document.title}</b><span>{document.document_type} · {document.status}</span>{document.instrument && <span>{labels.kind[document.instrument.kind] || document.instrument.kind} · {legalStatusLabel(labels, document.instrument.status)}{document.instrument.version_label ? ` · ${document.instrument.version_label}` : ""}</span>}</div>)}
      {!context.documents?.length && <p className="section-copy">{t("legal.inspector.overview.noDocuments")}</p>}
      {warnings.map(warning => <p className="inline-error" key={warning}>⚠ {warning}</p>)}
      <div className="preview-actions"><Button label={t("legal.inspector.overview.viewConnected")} size="sm" variant="secondary" onClick={() => onFocus(1)}/><Button label={t("legal.inspector.overview.analyzeImpact")} size="sm" variant="secondary" onClick={onImpact}/></div>
    </div>}
    {!loading && tab === "evidence" && <div className="inspector-section">{evidence.length ? evidence.map((source, index) => <details className="inspector-evidence" open={index === 0} key={`${source.document_id}-${index}`}><summary>{source.title}</summary><p>{source.excerpt || t("legal.inspector.noExcerpt")}</p></details>) : <p className="section-copy">{t("legal.inspector.evidence.empty")}</p>}</div>}
    {!loading && tab === "relations" && <div className="inspector-section">{[...incoming, ...outgoing].length ? <ul className="inspector-relations">{[...incoming, ...outgoing].map(relation => <li key={relation.id}><b>{relation.direction === "incoming" ? "←" : "→"} {relationshipLabel(labels, relation.relationship_type)}</b><span>{relation.other_entity?.name || t("legal.inspector.unknownNode")} · {reviewStatusLabel(labels, relation.review_status)}</span><small>{relationshipOriginLabel(labels, relation.origin)}{relation.confidence == null ? "" : t("legal.inspector.confidencePercent", {pct: Math.round(relation.confidence * 100)})}</small>{relation.sources?.[0]?.excerpt && <small>{relation.sources[0].excerpt}</small>}</li>)}</ul> : <p className="section-copy">{t("legal.inspector.relations.empty")}</p>}</div>}
    {!loading && tab === "versions" && <div className="inspector-section">{versions.length ? versions.map(version => <div className="inspector-context-card" key={version.id}><b>{version.official_title || version.document_id}</b><span>{labels.kind[version.kind] || version.kind} · {legalStatusLabel(labels, version.status)} · {version.effective_from || t("legal.map.noDate")}</span></div>) : <p className="section-copy">{t("legal.inspector.versions.empty")}</p>}{data?.versions?.relations?.map(relation => <p className="section-copy" key={relation.id}><b>{relationshipLabel(labels, relation.relation)}</b> · {reviewStatusLabel(labels, relation.review_status)}{relation.evidence_quote ? ` · ${relation.evidence_quote}` : ""}</p>)}</div>}
    <p className="section-copy graph-help">{t("legal.inspector.footerHelp")}</p>
  </div>;
}

function GraphCanvas({knowledgeBaseId, entities, relationships, addEntity, addRelationship, impact, analyzeImpact, syncGraphFromDocuments, refreshGraph, isLegalGraph, legalGraphView, setLegalGraphView, queueLegalGraphRebuild, legalRebuildStatus, reviewLegalRelationship}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
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
  const [graphRelationshipFilter, setGraphRelationshipFilter] = useState("all");
  const [graphLayoutMode, setGraphLayoutMode] = useState("by-type");
  const [graphScope, setGraphScope] = useState("all");

  useEffect(() => { api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`).then(data => setLayout(Object.fromEntries(data.items.map(item => [item.entity_id, {x: item.x, y: item.y}])))).catch(() => setLayout({})); }, [knowledgeBaseId]);

  const graphEntityTypes = useMemo(() => [...new Set(entities.map(entity => entity.entity_type).filter(Boolean))].sort((a, b) => {
    const aOrder = entityTypeOrder(a); const bOrder = entityTypeOrder(b);
    return (aOrder < 0 ? Number.MAX_SAFE_INTEGER : aOrder) - (bOrder < 0 ? Number.MAX_SAFE_INTEGER : bOrder) || a.localeCompare(b);
  }), [entities]);
  const graphRelationshipTypes = useMemo(() => [...new Set(relationships.map(relationship => relationship.relationship_type).filter(Boolean))].sort(), [relationships]);
  const filteredEntities = useMemo(() => entities.filter(entity => (!graphSearch.trim() || `${entity.name} ${entity.entity_type}`.toLowerCase().includes(graphSearch.trim().toLowerCase())) && (graphTypeFilter === "all" || entity.entity_type === graphTypeFilter) && (graphStatusFilter === "all" || entity.review_status === graphStatusFilter)), [entities, graphSearch, graphTypeFilter, graphStatusFilter]);
  const filteredEntityIds = useMemo(() => new Set(filteredEntities.map(entity => entity.id)), [filteredEntities]);
  const filteredRelationships = useMemo(() => relationships.filter(relationship => (graphRelationshipFilter === "all" || relationship.relationship_type === graphRelationshipFilter) && filteredEntityIds.has(relationship.source_entity_id) && filteredEntityIds.has(relationship.target_entity_id)), [relationships, graphRelationshipFilter, filteredEntityIds]);
  const visibleEntityIds = useMemo(() => {
    if (graphScope !== "focus" || !selectedEntityId || !filteredEntityIds.has(selectedEntityId)) return filteredEntityIds;
    const neighbours = new Map();
    filteredRelationships.forEach(relationship => {
      if (!neighbours.has(relationship.source_entity_id)) neighbours.set(relationship.source_entity_id, []);
      if (!neighbours.has(relationship.target_entity_id)) neighbours.set(relationship.target_entity_id, []);
      neighbours.get(relationship.source_entity_id).push(relationship.target_entity_id);
      neighbours.get(relationship.target_entity_id).push(relationship.source_entity_id);
    });
    const ids = new Set([selectedEntityId]); let frontier = [selectedEntityId];
    for (let depth = 0; depth < graphDepth; depth += 1) {
      frontier = frontier.flatMap(id => neighbours.get(id) || []).filter(id => !ids.has(id));
      frontier.forEach(id => ids.add(id));
    }
    return ids;
  }, [graphScope, selectedEntityId, filteredEntityIds, filteredRelationships, graphDepth]);
  const visibleEntities = useMemo(() => filteredEntities.filter(entity => visibleEntityIds.has(entity.id)), [filteredEntities, visibleEntityIds]);
  const visibleRelationships = useMemo(() => filteredRelationships.filter(relationship => visibleEntityIds.has(relationship.source_entity_id) && visibleEntityIds.has(relationship.target_entity_id)), [filteredRelationships, visibleEntityIds]);

  useEffect(() => {
    if (selectedEntityId && !filteredEntityIds.has(selectedEntityId)) {
      setSelectedEntityId("");
      setGraphScope("all");
    }
  }, [selectedEntityId, filteredEntityIds]);
  useEffect(() => {
    if (selectedRelationshipId && !visibleRelationships.some(relationship => relationship.id === selectedRelationshipId)) {
      setSelectedRelationshipId("");
    }
  }, [selectedRelationshipId, visibleRelationships]);

  useEffect(() => {
    const degree = {};
    visibleRelationships.forEach(relationship => {
      degree[relationship.source_entity_id] = (degree[relationship.source_entity_id] || 0) + 1;
      degree[relationship.target_entity_id] = (degree[relationship.target_entity_id] || 0) + 1;
    });
    const ordered = [...visibleEntities].sort((a, b) => (degree[b.id] || 0) - (degree[a.id] || 0) || a.name.localeCompare(b.name));
    const rankById = Object.fromEntries(ordered.map((entity, index) => [entity.id, index]));
    const groupedEntities = new Map();
    visibleEntities.forEach(entity => {
      const type = entity.entity_type || "Other";
      if (!groupedEntities.has(type)) groupedEntities.set(type, []);
      groupedEntities.get(type).push(entity);
    });
    const groups = [...groupedEntities.keys()].sort((a, b) => {
      const aOrder = entityTypeOrder(a); const bOrder = entityTypeOrder(b);
      return (aOrder < 0 ? Number.MAX_SAFE_INTEGER : aOrder) - (bOrder < 0 ? Number.MAX_SAFE_INTEGER : bOrder) || a.localeCompare(b);
    });
    const typePositionById = {};
    let groupX = 140;
    groups.forEach(type => {
      const group = groupedEntities.get(type).sort((a, b) => a.name.localeCompare(b.name));
      const columns = Math.min(8, Math.max(1, Math.ceil(Math.sqrt(group.length))));
      group.forEach((entity, index) => {
        typePositionById[entity.id] = {x: groupX + (index % columns) * 190, y: 100 + Math.floor(index / columns) * 112};
      });
      groupX += columns * 190 + 80;
    });
    const selectedNeighbourIds = new Set(selectedEntityId ? [selectedEntityId] : []);
    if (selectedEntityId) visibleRelationships.forEach(relationship => {
      if (relationship.source_entity_id === selectedEntityId) selectedNeighbourIds.add(relationship.target_entity_id);
      if (relationship.target_entity_id === selectedEntityId) selectedNeighbourIds.add(relationship.source_entity_id);
    });
    setNodes(current => {
      const currentById = new Map(current.map(node => [node.id, node]));
      return visibleEntities.map((entity, index) => {
      const existing = currentById.get(entity.id);
      const rank = rankById[entity.id] ?? index;
      // Keep the highest-degree node in the centre and distribute the rest
      // across multiple rings. A single tight ring works for small graphs but
      // causes Thai labels and edge endpoints to overlap as legal instruments
      // grow beyond a handful of provisions.
      const outerIndex = Math.max(0, rank - 1);
      const slotsPerRing = 8;
      const ringIndex = Math.floor(outerIndex / slotsPerRing);
      const slotIndex = outerIndex % slotsPerRing;
      const slotCount = Math.min(slotsPerRing, Math.max(1, visibleEntities.length - 1 - ringIndex * slotsPerRing));
      const angle = ((slotIndex / slotCount) * Math.PI * 2) - Math.PI / 2;
      const radius = 300 + ringIndex * 190;
      const automaticPosition = rank === 0 ? {x: 640, y: 480} : {x: 640 + Math.cos(angle) * radius, y: 480 + Math.sin(angle) * radius};
      const position = graphLayoutMode === "saved" ? layout[entity.id] || existing?.position || automaticPosition : graphLayoutMode === "by-type" ? typePositionById[entity.id] : automaticPosition;
      return {id: entity.id, type: "knowledge", position, data: {label: entity.name, entityType: entity.entity_type, documentId: entity.attributes?.document_id, reviewStatus: entity.review_status, isLegal: entity.is_legal, isConnected: selectedEntityId && selectedNeighbourIds.has(entity.id), isMuted: selectedEntityId && !selectedNeighbourIds.has(entity.id)}};
      });
    });
  }, [visibleEntities, visibleRelationships, layout, setNodes, graphLayoutMode, selectedEntityId]);

  useEffect(() => {
    const nodesById = Object.fromEntries(nodes.map(node => [node.id, node]));
    setEdges(visibleRelationships.filter(relationship => nodesById[relationship.source_entity_id] && nodesById[relationship.target_entity_id]).map(relationship => {
      const isDependency = /DEPEND|RUNS_ON|USES/i.test(relationship.relationship_type);
      const reviewStatus = relationship.review_status || "verified";
      const statusColor = reviewStatus === "suggested" ? "#d58b14" : reviewStatus === "rejected" ? "#9a6a6a" : relationship.origin === "manual" ? "#65439a" : (isDependency ? "#56328d" : "#008c96");
      const handles = connectionHandles(nodesById[relationship.source_entity_id], nodesById[relationship.target_entity_id]);
      const selectedPath = selectedEntityId && (relationship.source_entity_id === selectedEntityId || relationship.target_entity_id === selectedEntityId);
      const showLabel = relationship.id === selectedRelationshipId || Boolean(selectedPath && selectedEntityId);
      return {id: relationship.id, source: relationship.source_entity_id, target: relationship.target_entity_id, ...handles, label: showLabel ? relationshipLabel(labels, relationship.relationship_type) : undefined, type: "smoothstep", markerEnd: {type: MarkerType.ArrowClosed, color: statusColor}, style: {stroke: statusColor, strokeWidth: selectedPath ? 2.8 : reviewStatus === "suggested" ? 2.2 : 1.8, strokeDasharray: reviewStatus === "suggested" ? "7 5" : reviewStatus === "rejected" ? "3 5" : undefined, opacity: selectedEntityId && !selectedPath ? .12 : reviewStatus === "rejected" ? .55 : 1}, labelStyle: {fill: statusColor, fontWeight: 700, fontSize: 11}, labelBgStyle: {fill: "#ffffff", fillOpacity: 0.96}};
    }));
  }, [nodes, visibleRelationships, selectedRelationshipId, selectedEntityId, setEdges, labels]);

  useEffect(() => { if (visibleEntities.length) requestAnimationFrame(() => fitView({padding: 0.3, duration: 280})); }, [visibleEntities.length, graphLayoutMode, graphScope, fitView]);

  const selectedEntity = entities.find(entity => entity.id === selectedEntityId);
  const selectedRelationship = relationships.find(relationship => relationship.id === selectedRelationshipId);
  const entityNamesById = useMemo(() => Object.fromEntries(entities.map(entity => [entity.id, entity.name])), [entities]);
  useEffect(() => { if (selectedEntity) { setEditName(selectedEntity.name); setEditEntityType(selectedEntity.entity_type); } }, [selectedEntity]);
  useEffect(() => { if (selectedRelationship) setEditRelationshipType(selectedRelationship.relationship_type); }, [selectedRelationship]);
  useEffect(() => {
    if (!selectedEntity || !isLegalGraph) { setInspectorData(null); return undefined; }
    let active = true; setInspectorLoading(true); setInspectorTab("overview");
    api(`/v1/entities/${selectedEntity.id}/inspector?depth=${graphDepth}`).then(data => { if (active) setInspectorData(data); }).catch(() => { if (active) setInspectorData({entity: selectedEntity, analysis: {warnings: [t("legal.graph.inspectorLoadError")]}}); }).finally(() => { if (active) setInspectorLoading(false); });
    return () => { active = false; };
  }, [selectedEntityId, isLegalGraph, graphDepth]);
  const focusSelected = async depth => {
    setGraphDepth(depth); if (!selectedEntityId) return;
    setGraphScope("focus");
    const neighbourhood = await api(`/v1/entities/${selectedEntityId}/graph?depth=${depth}`).catch(() => null);
    if (!neighbourhood) return;
    setGraphNotice(t("legal.graph.notice.showingConnected", {count: neighbourhood.nodes.length, depth}));
  };
  const onPaneClick = useCallback(event => {
    if (isLegalGraph && legalGraphView !== "manual") {
      setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(null); setGraphScope("all");
      setGraphNotice(t("legal.graph.notice.switchToManualAdd"));
      return;
    }
    const point = screenToFlowPosition({x: event.clientX, y: event.clientY});
    setSelectedEntityId(""); setSelectedRelationshipId(""); setGraphScope("all"); setDraftPosition(point); setEntityName(""); setGraphNotice(t("legal.graph.notice.nameNewEntity"));
  }, [screenToFlowPosition, isLegalGraph, legalGraphView]);
  const onNodeClick = useCallback((_, node) => { setSelectedEntityId(node.id); setSelectedRelationshipId(""); setDraftPosition(null); setGraphNotice(""); }, []);
  const onConnect = useCallback(async connection => {
    if (isLegalGraph && legalGraphView !== "manual") { setGraphNotice(t("legal.graph.notice.switchToManualConnect")); return; }
    if (!connection.source || !connection.target || connection.source === connection.target) return;
    const duplicate = relationships.some(item => item.source_entity_id === connection.source && item.target_entity_id === connection.target && item.relationship_type === relationshipType);
    if (duplicate) { setGraphNotice(t("legal.graph.notice.duplicateRelationship")); return; }
    const created = await addRelationship({sourceEntityId: connection.source, targetEntityId: connection.target, relationshipType});
    if (created) setGraphNotice(t("legal.graph.notice.connectionCreated", {type: relationshipType.replace(/_/g, " ")}));
  }, [addRelationship, relationshipType, relationships, isLegalGraph, legalGraphView, t]);
  const createNode = async event => {
    event.preventDefault(); const created = await addEntity({name: entityName, entityType});
    if (created) {
      const position = draftPosition || {x: 80, y: 80};
      setLayout(current => ({...current, [created.id]: position}));
      await api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`, {method: "PUT", body: JSON.stringify({items: [{entity_id: created.id, x: position.x, y: position.y}]})});
      setDraftPosition(null); setSelectedEntityId(created.id); setGraphNotice(t("legal.graph.notice.entityAdded"));
    }
  };
  const runImpactForSelected = async event => {
    event.preventDefault(); if (!selectedEntity) return; await analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario});
  };
  const saveLayout = useCallback(async nextNodes => {
    try { await api(`/v1/knowledge-bases/${knowledgeBaseId}/graph-layout`, {method: "PUT", body: JSON.stringify({items: nextNodes.map(node => ({entity_id: node.id, x: node.position.x, y: node.position.y}))})}); }
    catch { setGraphNotice(t("legal.graph.notice.layoutSaveFailed")); }
  }, [knowledgeBaseId, t]);
  const onNodeDragStop = useCallback((_, __, nextNodes) => {
    const positionedNodes = nextNodes?.length ? nextNodes : nodes;
    setLayout(current => ({...current, ...Object.fromEntries(positionedNodes.map(node => [node.id, node.position]))}));
    setGraphLayoutMode("saved"); saveLayout(positionedNodes);
  }, [nodes, saveLayout]);
  const updateSelectedEntity = async event => {
    event.preventDefault(); if (!selectedEntity || !editName.trim()) return;
    await api(`/v1/entities/${selectedEntity.id}`, {method: "PATCH", body: JSON.stringify({name: editName.trim(), entity_type: editEntityType})});
    await refreshGraph(); setGraphNotice(t("legal.graph.notice.entitySaved"));
  };
  const deleteSelectedEntity = async () => {
    if (!selectedEntity || !window.confirm(t("legal.graph.confirm.deleteEntity", {name: selectedEntity.name}))) return;
    await api(`/v1/entities/${selectedEntity.id}`, {method: "DELETE"}); setSelectedEntityId(""); await refreshGraph(); setGraphNotice(t("legal.graph.notice.entityDeleted"));
  };
  const selectEdge = (_, edge) => { setSelectedRelationshipId(edge.id); setSelectedEntityId(""); setDraftPosition(null); setGraphNotice(""); };
  const updateSelectedRelationship = async event => {
    event.preventDefault(); if (!selectedRelationship) return;
    await api(`/v1/relationships/${selectedRelationship.id}`, {method: "PATCH", body: JSON.stringify({relationship_type: editRelationshipType})});
    await refreshGraph(); setGraphNotice(t("legal.graph.notice.relationshipSaved"));
  };
  const deleteSelectedRelationship = async () => {
    if (!selectedRelationship || !window.confirm(t("legal.graph.confirm.deleteRelationship", {type: selectedRelationship.relationship_type.replace(/_/g, " ")}))) return;
    await api(`/v1/relationships/${selectedRelationship.id}`, {method: "DELETE"}); setSelectedRelationshipId(""); await refreshGraph(); setGraphNotice(t("legal.graph.notice.relationshipDeleted"));
  };
  const syncGraph = async () => {
    const result = await syncGraphFromDocuments();
    if (result) setGraphNotice(result.entities || result.relationships ? t("legal.graph.notice.syncSuccess") : t("legal.graph.notice.syncEmpty"));
  };

  const closeInspector = () => {
    setSelectedEntityId(""); setSelectedRelationshipId(""); setDraftPosition(null); setGraphScope("all"); setGraphNotice("");
  };
  const isInspectorOpen = Boolean(draftPosition || selectedEntity || selectedRelationship || graphNotice);
  const selectedEntityPanel = isLegalGraph && legalGraphView !== "manual" ? <LegalInspector entity={selectedEntity} data={inspectorData} loading={inspectorLoading} tab={inspectorTab} setTab={setInspectorTab} onImpact={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})} onFocus={focusSelected}/> : <form className="form-stack" onSubmit={updateSelectedEntity}><p className="eyebrow">{t("legal.graph.entityForm.eyebrow")}</p><h2>{t("legal.graph.entityForm.title")}</h2><TextInput label={t("legal.graph.entityForm.nameLabel")} value={editName} onChange={setEditName} isRequired/><Selector label={t("legal.graph.entityForm.typeLabel")} value={editEntityType} onChange={setEditEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: entityTypeLabel(t, type)}))} size="md"/><p className="section-copy graph-help">{t("legal.graph.entityForm.dragHelp", {type: relationshipLabel(labels, relationshipType)})}</p><Button label={t("legal.graph.entityForm.save")} type="submit" variant="primary" isDisabled={!editName.trim()}/><div className="form-stack graph-impact-form"><TextInput label={t("legal.graph.entityForm.scenarioLabel")} value={scenario} onChange={setScenario} placeholder={t("legal.graph.entityForm.scenarioPlaceholder")} isRequired/><Button label={t("legal.graph.entityForm.analyzeImpact")} type="button" variant="secondary" onClick={() => analyzeImpact({subject: selectedEntity.name, entityId: selectedEntity.id, scenario})}/></div><Button label={t("legal.graph.entityForm.delete")} type="button" variant="destructive" onClick={deleteSelectedEntity}/></form>;
  return <section className="graph-workspace"><div className="graph-toolbar"><div className="graph-summary"><Badge label={t("graph.nodesCount", {count: entities.length})} variant="info"/><Badge label={t("graph.connectionsCount", {count: relationships.length})} variant="neutral"/></div><div className="graph-toolbar-controls"><label className="relationship-picker"><span>{t("graph.controls.findNode")}</span><input value={graphSearch} onChange={event => setGraphSearch(event.target.value)} placeholder={t("graph.controls.findNodePlaceholder")}/></label><Selector label={t("graph.controls.nodeType")} value={graphTypeFilter} onChange={setGraphTypeFilter} options={[{value: "all", label: t("common.allTypes")}, ...graphEntityTypes.map(type => ({value: type, label: isLegalGraph ? legalEntityLabel(labels, type) : entityTypeLabel(t, type)}))]}/><Selector label={t("graph.controls.relationshipType")} value={graphRelationshipFilter} onChange={setGraphRelationshipFilter} options={[{value: "all", label: t("graph.controls.allRelationships")}, ...graphRelationshipTypes.map(type => ({value: type, label: relationshipLabel(labels, type)}))]}/>{isLegalGraph ? <><Selector label={t("legal.graph.toolbar.viewLabel")} value={legalGraphView} onChange={setLegalGraphView} options={[{value: "verified", label: t("legal.graph.view.verified")}, {value: "suggested", label: t("legal.graph.view.suggested")}, {value: "manual", label: t("legal.graph.view.manual")}, {value: "all", label: t("legal.graph.view.all")}]}/><Selector label={t("legal.graph.toolbar.reviewStatusLabel")} value={graphStatusFilter} onChange={setGraphStatusFilter} options={[{value: "all", label: t("common.allStatuses")}, {value: "verified", label: labels.reviewStatus.verified}, {value: "suggested", label: labels.reviewStatus.suggested}, {value: "rejected", label: labels.reviewStatus.rejected}]}/><Button label={legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status) ? t("legal.graph.toolbar.rebuilding") : t("legal.graph.toolbar.rebuild")} variant="secondary" size="sm" onClick={queueLegalGraphRebuild} isDisabled={Boolean(legalRebuildStatus && ["queued", "running"].includes(legalRebuildStatus.status))}/></> : <><Selector label={t("legal.graph.toolbar.newRelationshipType")} value={relationshipType} onChange={setRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: relationshipLabel(labels, type)}))}/><Button label={t("legal.graph.toolbar.importFromDocuments")} variant="secondary" size="sm" onClick={syncGraph}/></>}<div className="graph-layout-switch" role="group" aria-label={t("graph.controls.layout")}><Button label={t("graph.controls.layoutByType")} className={graphLayoutMode === "by-type" ? "selected" : ""} variant="ghost" size="sm" aria-pressed={graphLayoutMode === "by-type"} onClick={() => setGraphLayoutMode("by-type")}/><Button label={t("graph.controls.layoutRadial")} className={graphLayoutMode === "radial" ? "selected" : ""} variant="ghost" size="sm" aria-pressed={graphLayoutMode === "radial"} onClick={() => setGraphLayoutMode("radial")}/><Button label={t("graph.controls.layoutSaved")} className={graphLayoutMode === "saved" ? "selected" : ""} variant="ghost" size="sm" aria-pressed={graphLayoutMode === "saved"} onClick={() => setGraphLayoutMode("saved")}/></div><Button label={graphScope === "focus" ? t("graph.controls.clearFocus") : t("graph.controls.focusSelected")} variant={graphScope === "focus" ? "secondary" : "ghost"} size="sm" onClick={() => setGraphScope(scope => scope === "focus" ? "all" : "focus")} isDisabled={!selectedEntityId}/>{graphScope === "focus" && <Selector label={t("graph.controls.focusDepth")} value={String(graphDepth)} onChange={value => setGraphDepth(Number(value))} options={[{value: "1", label: t("graph.controls.focusDepthOne")}, {value: "2", label: t("graph.controls.focusDepthTwo")}]}/>}<Button label={t("legal.graph.toolbar.fitView")} variant="ghost" size="sm" onClick={() => fitView({padding: 0.24, duration: 280})}/></div></div>
    <div className="graph-explorer-legend" role="group" aria-label={t("graph.controls.legend")}><span className="graph-explorer-legend-title">{t("graph.controls.legend")}</span>{graphEntityTypes.map(type => { const visual = graphTypeVisual(type); const Icon = visual.Icon; const selected = graphTypeFilter === type; return <button type="button" key={type} className={selected ? "selected" : ""} aria-pressed={selected} onClick={() => setGraphTypeFilter(current => current === type ? "all" : type)}><i style={{"--graph-node-color": visual.color, "--graph-node-tint": visual.tint}}><Icon size={14} weight="duotone" aria-hidden="true"/></i><span>{isLegalGraph ? legalEntityLabel(labels, type) : entityTypeLabel(t, type)}</span></button>; })}<span className="graph-explorer-result-count">{t("graph.controls.shown", {nodes: visibleEntities.length, relationships: visibleRelationships.length})}</span></div>
    {isLegalGraph && <div className="graph-status-legend" role="note"><span className="graph-status-legend-title">{t("legal.graph.statusLegend.title")}</span><span><i className="verified" aria-hidden="true"/>{t("legal.graph.statusLegend.solidLine", {status: labels.reviewStatus.verified})}</span><span><i className="suggested" aria-hidden="true"/>{t("legal.graph.statusLegend.dashedLine", {status: labels.reviewStatus.suggested})}</span><span><i className="manual" aria-hidden="true"/>{t("legal.graph.statusLegend.purpleLine", {status: labels.relationshipOrigin.manual})}</span><span className="graph-status-legend-help">{t("legal.graph.statusLegend.help")}</span></div>}
    <div className="graph-layout"><div className={`graph-canvas ${isLegalGraph && entities.length > 10 ? "graph-canvas-dense" : ""}`}><ReactFlow nodes={nodes} edges={edges} nodeTypes={graphNodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodeClick={onNodeClick} onPaneClick={onPaneClick} onNodeDragStop={onNodeDragStop} onEdgeClick={selectEdge} onConnect={onConnect} fitView fitViewOptions={{padding: 0.3}} minZoom={0.25} maxZoom={2} nodesConnectable connectionMode="loose" connectionRadius={24} defaultEdgeOptions={{type: "smoothstep"}}><Background gap={20} size={1} color="#b9cbd3"/><MiniMap pannable zoomable nodeColor="#2c7282"/><Controls showInteractive={false}/></ReactFlow></div>
      <aside className={`graph-inspector ${isInspectorOpen ? "open" : "closed"}`}>{isInspectorOpen && <button type="button" className="graph-inspector-close" onClick={closeInspector} aria-label={t("legal.graph.inspector.closeAriaLabel")} style={{position: "absolute", top: 12, right: 14, border: 0, background: "transparent", color: "#52717a", fontSize: "1.5rem", lineHeight: 1, cursor: "pointer"}}>×</button>}{draftPosition ? <form className="form-stack" onSubmit={createNode}><p className="eyebrow">{t("legal.graph.newNode.eyebrow")}</p><h2>{t("legal.graph.newNode.title")}</h2><p className="section-copy">{t("legal.graph.newNode.description")}</p><TextInput label={t("legal.graph.newNode.nameLabel")} value={entityName} onChange={setEntityName} placeholder={t("legal.graph.newNode.namePlaceholder")} isRequired hasAutoFocus/><Selector label={t("legal.graph.newNode.typeLabel")} value={entityType} onChange={setEntityType} options={ENTITY_TYPES.map(type => ({value: type, label: entityTypeLabel(t, type)}))} size="md"/><Button label={t("legal.graph.newNode.submit")} type="submit" variant="primary" isDisabled={!entityName.trim()}/></form> : selectedEntity ? selectedEntityPanel : selectedRelationship ? <form className="form-stack" onSubmit={updateSelectedRelationship}><p className="eyebrow">{selectedRelationship.review_status === "suggested" ? t("legal.graph.relationship.suggestedEyebrow") : t("legal.graph.relationship.selectedEyebrow")}</p><div className="relationship-heading"><h2>{relationshipLabel(labels, selectedRelationship.relationship_type)}</h2><Badge label={reviewStatusLabel(labels, selectedRelationship.review_status)} variant={reviewBadgeVariant(selectedRelationship.review_status)}/></div><p className="section-copy"><b>{t("legal.graph.relationship.sourceLabel")}</b> {relationshipOriginLabel(labels, selectedRelationship.origin)}{selectedRelationship.confidence == null ? "" : ` · ${t("legal.inspector.confidencePercent", {pct: Math.round(selectedRelationship.confidence * 100)})}`}</p><div className="relationship-direction" aria-label={t("legal.graph.relationship.directionAriaLabel")}><span className="relationship-entity"><b>{entityNamesById[selectedRelationship.source_entity_id] || t("legal.graph.relationship.unknownSource")}</b><code>{String(selectedRelationship.source_entity_id).slice(0, 8)}</code></span><span aria-hidden="true">→</span><span className="relationship-entity"><b>{entityNamesById[selectedRelationship.target_entity_id] || t("legal.graph.relationship.unknownTarget")}</b><code>{String(selectedRelationship.target_entity_id).slice(0, 8)}</code></span></div>{selectedRelationship.sources?.length ? <div className="legal-evidence"><b>{t("legal.graph.relationship.evidenceHeading")}</b>{selectedRelationship.sources.map(source => <details key={`${source.document_id}-${source.excerpt}`}><summary>{source.title}</summary><p>{source.excerpt || t("legal.inspector.noExcerpt")}</p></details>)}</div> : <p className="section-copy">{t("legal.graph.relationship.noEvidence")}</p>}{selectedRelationship.origin === "ai_suggestion" && selectedRelationship.review_status === "suggested" ? <div className="preview-actions"><Button label={t("legal.graph.relationship.approve")} type="button" variant="primary" onClick={() => reviewLegalRelationship(selectedRelationship.id, "verified")}/><Button label={t("legal.graph.relationship.reject")} type="button" variant="destructive" onClick={() => reviewLegalRelationship(selectedRelationship.id, "rejected")}/></div> : (!isLegalGraph || legalGraphView === "manual") && <><Selector label={t("legal.graph.relationship.typeLabel")} value={editRelationshipType} onChange={setEditRelationshipType} options={RELATIONSHIP_TYPES.map(type => ({value: type, label: relationshipLabel(labels, type)}))} size="md"/><Button label={t("legal.graph.relationship.save")} type="submit" variant="primary"/><Button label={t("legal.graph.relationship.delete")} type="button" variant="destructive" onClick={deleteSelectedRelationship}/></>}</form> : null}{graphNotice && <p className="graph-notice" role="status">{graphNotice}</p>}</aside></div>
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

function buildIngestCurl({apiBase, kbId, token}) {
  return `# 0. List the Knowledge Base this token can write to (0 or 1 item)
curl "${apiBase}/ingest/knowledge-bases" \\
  -H "Authorization: Bearer ${token}"

# 1. Upload a single document (202 = queued for processing)
curl -X POST "${apiBase}/ingest/knowledge-bases/${kbId}/documents" \\
  -H "Authorization: Bearer ${token}" \\
  -F "file=@./contract.pdf" \\
  -F "title=Supply agreement 2026" \\
  -F "document_type=contract"

# 2. Upload up to 20 documents in one request (per-file results)
curl -X POST "${apiBase}/ingest/knowledge-bases/${kbId}/documents/batch" \\
  -H "Authorization: Bearer ${token}" \\
  -F "files=@./a.pdf" \\
  -F "files=@./b.docx" \\
  -F "document_type=general"

# 3. Poll one document until it leaves the queue
curl "${apiBase}/ingest/documents/DOCUMENT_ID" \\
  -H "Authorization: Bearer ${token}"`;
}

function buildIngestPython({apiBase, kbId, token}) {
  return `import os, time, requests

API_BASE = "${apiBase}"
KB_ID = "${kbId}"
HEADERS = {"Authorization": f"Bearer {os.environ['SOFTNIX_INGEST_TOKEN']}"}
TERMINAL = {"completed", "failed", "ocr_required"}


def writable_knowledge_base():
    response = requests.get(f"{API_BASE}/ingest/knowledge-bases", headers=HEADERS, timeout=30)
    response.raise_for_status()
    items = response.json()["items"]
    return items[0] if items else None  # None: KB was removed or never configured


def upload(path, document_type="general", title=None):
    with open(path, "rb") as handle:
        response = requests.post(
            f"{API_BASE}/ingest/knowledge-bases/{KB_ID}/documents",
            headers=HEADERS,
            files={"file": (os.path.basename(path), handle)},
            data={"document_type": document_type, **({"title": title} if title else {})},
            timeout=120,
        )
    if response.status_code == 409:
        return None  # FILE_DUPLICATE: already ingested, nothing to do
    response.raise_for_status()
    return response.json()["document_id"]


def wait_for(document_id, interval=5, max_interval=60):
    while True:
        document = requests.get(f"{API_BASE}/ingest/documents/{document_id}",
                                headers=HEADERS, timeout=30).json()
        if document["status"] in TERMINAL:
            return document
        time.sleep(interval)
        interval = min(interval * 2, max_interval)  # back off on long documents


document_id = upload("./contract.pdf", "contract", "Supply agreement 2026")
if document_id:
    print(wait_for(document_id))`;
}

function buildIngestNode({apiBase, kbId, token}) {
  return `import {readFile} from "node:fs/promises";
import {basename} from "node:path";

const API_BASE = "${apiBase}";
const KB_ID = "${kbId}";
const HEADERS = {Authorization: \`Bearer \${process.env.SOFTNIX_INGEST_TOKEN}\`};
const TERMINAL = new Set(["completed", "failed", "ocr_required"]);

async function writableKnowledgeBase() {
  const response = await fetch(\`\${API_BASE}/ingest/knowledge-bases\`, {headers: HEADERS});
  const {items} = await response.json();
  return items[0] || null; // null: KB was removed or never configured
}

async function upload(path, documentType = "general", title) {
  const body = new FormData();
  body.append("file", new Blob([await readFile(path)]), basename(path));
  body.append("document_type", documentType);
  if (title) body.append("title", title);
  const response = await fetch(\`\${API_BASE}/ingest/knowledge-bases/\${KB_ID}/documents\`, {method: "POST", headers: HEADERS, body});
  if (response.status === 409) return null; // FILE_DUPLICATE: already ingested
  if (!response.ok) throw new Error(JSON.stringify(await response.json()));
  return (await response.json()).document_id;
}

async function waitFor(documentId, interval = 5000, maxInterval = 60000) {
  for (;;) {
    const response = await fetch(\`\${API_BASE}/ingest/documents/\${documentId}\`, {headers: HEADERS});
    const document = await response.json();
    if (TERMINAL.has(document.status)) return document;
    await new Promise(resolve => setTimeout(resolve, interval));
    interval = Math.min(interval * 2, maxInterval); // back off on long documents
  }
}

const documentId = await upload("./contract.pdf", "contract", "Supply agreement 2026");
if (documentId) console.log(await waitFor(documentId));`;
}

const INGEST_SCOPE = "documents:write";

const INGEST_SNIPPETS = {
  curl: {label: "cURL", build: buildIngestCurl},
  python: {label: "Python", build: buildIngestPython},
  node: {label: "Node.js", build: buildIngestNode},
};
const isIngestToken = token => Boolean(token.allowed_scopes?.includes(INGEST_SCOPE));
const INGEST_ENDPOINTS = [
  {method: "GET", path: "/ingest/knowledge-bases", descriptionKey: "ingestTokens.endpoints.listKb"},
  {method: "POST", path: "/ingest/knowledge-bases/{kb_id}/documents", descriptionKey: "ingestTokens.endpoints.uploadOne"},
  {method: "POST", path: "/ingest/knowledge-bases/{kb_id}/documents/batch", descriptionKey: "ingestTokens.endpoints.uploadBatch"},
  {method: "GET", path: "/ingest/knowledge-bases/{kb_id}/documents", descriptionKey: "ingestTokens.endpoints.listDocuments"},
  {method: "GET", path: "/ingest/documents/{document_id}", descriptionKey: "ingestTokens.endpoints.getDocument"},
  {method: "GET", path: "/ingest/documents/{document_id}/jobs", descriptionKey: "ingestTokens.endpoints.listJobs"},
];

function McpTokensView({selectedKb, knowledgeBases, tokens, auditLogs, loadAccess, createMcpToken, rotateMcpToken, changeTokenState}) {
  const {t} = useLanguage();
  const allTools = ["search_knowledge", "document_inventory_summary", "find_entities", "analyze_relationships", "analyze_impact", "get_sources", "resolve_legal_context", "get_legal_instrument", "get_provision_history"];
  const activeKnowledgeBases = knowledgeBases.filter(kb => kb.status === "active");
  const kbNames = useMemo(() => Object.fromEntries(knowledgeBases.map(kb => [kb.id, kb.name])), [knowledgeBases]);
  const mcpTokens = useMemo(() => tokens.filter(token => !isIngestToken(token)), [tokens]);
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
  const copy = async (value, label) => { try { await navigator.clipboard.writeText(value); setCopyError(""); setCopied(label); window.setTimeout(() => setCopied(""), 1800); } catch { setCopyError(t("tokens.error.copyFailed")); } };
  const refreshAccess = async () => { setAccessLoading(true); setAccessLoadError(""); try { const result = await loadAccess(); if (result?.errors?.length) setAccessLoadError(result.errors.join(" · ")); } catch (error) { setAccessLoadError(error.message || t("tokens.error.loadAccessFailed")); } finally { setAccessLoading(false); } };
  const loadOperations = async () => { try { const [ready, projection] = await Promise.all([api("/v1/system/status"), api("/v1/system/graph-projection")]); setOperations({ready, projection}); setOperationsError(""); } catch (error) { setOperationsError(error.message || t("tokens.error.loadSystemStatusFailed")); } };
  useEffect(() => { refreshAccess(); loadOperations(); return () => { if (secretTimer.current) window.clearTimeout(secretTimer.current); }; }, []);
  useEffect(() => { const activeIds = new Set(activeKnowledgeBases.map(kb => kb.id)); setSelectedKbs(current => { const retained = current.filter(id => activeIds.has(id)); if (retained.length || !selectedKb || !activeIds.has(selectedKb.id)) return retained; return [selectedKb.id]; }); }, [selectedKb, knowledgeBases]);
  const create = async event => { event.preventDefault(); setIsLoading(true); setFormError(""); try { const result = await createMcpToken({name, allowed_knowledge_base_ids: selectedKbs, allowed_tools: tools, allowed_scopes: [], allowed_ingest_knowledge_base_id: null, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null, requests_per_minute: Number(rpm), max_concurrent_requests: Number(concurrency), query_timeout_seconds: Number(timeout)}); revealSecret(result.token); setName(""); } catch (error) { setFormError(error.message || t("mcpTokens.error.createFailed")); } finally { setIsLoading(false); } };
  const rotate = async token => { if (!window.confirm(t("tokens.confirm.rotate", {name: token.name}))) return; setMutatingTokenId(token.id); setActionError(""); try { const result = await rotateMcpToken(token.id); revealSecret(result.token); await copy(result.token, "token"); } catch (error) { setActionError(error.message || t("tokens.error.rotateFailed")); } finally { setMutatingTokenId(""); } };
  const changeState = async (token, action) => { if (action === "revoke" && !window.confirm(t("mcpTokens.confirm.revoke", {name: token.name}))) return; if (action === "disable" && !window.confirm(t("mcpTokens.confirm.disable", {name: token.name}))) return; setMutatingTokenId(`${token.id}:${action}`); setActionError(""); try { await changeTokenState(token.id, action); } catch (error) { setActionError(error.message || t("tokens.error.changeStateFailed")); } finally { setMutatingTokenId(""); } };
  const visibleTokens = mcpTokens.filter(token => { const matchStatus = tokenFilter === "all" || token.status === tokenFilter; const needle = tokenSearch.trim().toLocaleLowerCase(); const matchSearch = !needle || `${token.name} ${token.token_prefix}`.toLocaleLowerCase().includes(needle); return matchStatus && matchSearch; });
  const statusLabel = {active: t("common.active"), inactive: t("common.inactive"), revoked: t("common.revoked")};
  return <><PageHeading eyebrow={t("mcpTokens.eyebrow")} title={t("mcpTokens.title")} description={t("mcpTokens.description")} actions={<Button label={t("tokens.refreshStatus")} variant="ghost" isLoading={accessLoading} onClick={() => { refreshAccess(); loadOperations(); }}/>}/>{(accessLoadError || operationsError || copyError) && <p className="inline-error access-error" role="alert">{accessLoadError || operationsError || copyError}</p>}<section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operationsError ? t("tokens.status.unavailable") : operations?.ready?.status || t("tokens.status.checking")}</b><span>{operations ? t("tokens.status.dependenciesOnline", {count: Object.keys(operations.ready.dependencies || {}).length}) : operationsError || t("tokens.status.loadingDependencies")}</span></div></div><div className="mcp-endpoint"><span>{t("mcpTokens.endpointLabel")}</span><code>{mcpUrl}</code><button type="button" onClick={() => copy(mcpUrl, "endpoint")}>{t("common.copy")}</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">{t("tokens.step1Eyebrow")}</p><h2>{t("tokens.createScopedTokenTitle")}</h2></div><Badge label={t("tokens.secretShownOnce")} variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label={t("tokens.form.nameLabel")} value={name} onChange={setName} placeholder={t("mcpTokens.form.namePlaceholder")} isRequired/><div className="scope-section"><div className="scope-heading"><b>{t("mcpTokens.form.kbAccessLabel")}</b>{activeKnowledgeBases.length > 0 && <button type="button" onClick={() => setSelectedKbs(activeKnowledgeBases.map(kb => kb.id))}>{t("mcpTokens.form.selectAll")}</button>}</div><p className="section-copy">{t("mcpTokens.form.kbAccessHelp")}</p><div className="scope-options">{activeKnowledgeBases.length ? activeKnowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${selectedKbs.includes(kb.id) ? "selected" : ""}`}><input type="checkbox" checked={selectedKbs.includes(kb.id)} onChange={() => toggle(kb.id, selectedKbs, setSelectedKbs)}/><span>{kb.name}</span></label>) : <p className="section-copy">{t("tokens.noActiveKb")}</p>}</div></div><div className="scope-section"><div className="scope-heading"><b>{t("mcpTokens.form.allowedToolsLabel")}</b><button type="button" onClick={() => setTools(allTools)}>{t("mcpTokens.form.enableAllTools")}</button></div><div className="tool-options">{allTools.map(tool => <label key={tool} className={`tool-option ${tools.includes(tool) ? "selected" : ""}`}><input type="checkbox" checked={tools.includes(tool)} onChange={() => toggle(tool, tools, setTools)}/><span>{tool.replace(/_/g, " ")}</span></label>)}</div></div><details className="advanced-options"><summary>{t("tokens.form.advancedLimits")}</summary><div className="limit-grid"><label>{t("tokens.form.expiryLabel")}<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>{t("tokens.form.rpmLabel")}<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>{t("tokens.form.concurrencyLabel")}<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>{t("tokens.form.timeoutLabel")}<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error" role="alert">{formError}</p>}<Button label={t("mcpTokens.form.submit")} type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !tools.length || !selectedKbs.length}/></form></Card><Card padding={4}><p className="eyebrow">{t("tokens.step2Eyebrow")}</p><h2>{t("mcpTokens.step2.title")}</h2><p className="section-copy">{t("mcpTokens.step2.description")}</p><div className="code-panel"><div className="code-panel-top"><b>{t("mcpTokens.step2.terminalLabel")}</b><button type="button" onClick={() => copy(cliCommand, "claude command")}>{copied === "claude command" ? t("common.copied") : t("tokens.copyCommand")}</button></div><pre>{cliCommand}</pre></div><ol className="mcp-steps"><li>{t("mcpTokens.step2.steps.createToken")}</li><li>{t("mcpTokens.step2.steps.paste")}</li><li>{t("mcpTokens.step2.steps.restartPrefix")} <code>/mcp</code> {t("mcpTokens.step2.steps.restartSuffix")} <code>softnix-knowledge</code> {t("mcpTokens.step2.steps.restartTail")}</li></ol><details className="json-config"><summary>{t("mcpTokens.mcpJson.preferPrefix")} <code>.mcp.json</code> {t("mcpTokens.mcpJson.preferSuffix")}</summary><p>{t("mcpTokens.mcpJson.storePrefix")} <code>SOFTNIX_MCP_TOKEN</code>{t("mcpTokens.mcpJson.storeSuffix")}</p><div className="code-panel"><div className="code-panel-top"><b>.mcp.json</b><button type="button" onClick={() => copy(jsonConfig, "json config")}>{copied === "json config" ? t("common.copied") : t("tokens.copyJson")}</button></div><pre>{jsonConfig}</pre></div></details><details className="json-config skill-config"><summary>{t("mcpTokens.addSkill")} <Badge label={t("mcpTokens.recommended")} variant="success"/></summary><p>{t("mcpTokens.skill.description1Prefix")} <b>{t("mcpTokens.skill.description1Bold")}</b> {t("mcpTokens.skill.description1Suffix")}</p><p className="section-copy">{t("mcpTokens.skill.description2Prefix")} <a href="https://agentskills.io" target="_blank" rel="noreferrer">{t("mcpTokens.skill.description2LinkLabel")}</a> {t("mcpTokens.skill.description2Middle")} <code>SKILL.md</code> {t("mcpTokens.skill.description2Middle2")} <code>softnix-knowledge</code> {t("mcpTokens.skill.description2Suffix")} <code>.claude/skills/softnix-knowledge/SKILL.md</code>.</p><div className="code-panel"><div className="code-panel-top"><b>SKILL.md</b><button type="button" onClick={() => copy(skillContent, "skill")}>{copied === "skill" ? t("common.copied") : t("mcpTokens.copySkill")}</button></div><pre className="skill-preview">{skillContent}</pre></div></details>{secret && <div className="token-reveal"><b>{t("tokens.newTokenCopyNow")}</b><code>{secret}</code><div className="token-reveal-actions"><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? t("common.copied") : t("tokens.copyToken")}</button><button type="button" onClick={hideSecret}>{t("tokens.hideToken")}</button></div></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">{t("tokens.managementEyebrow")}</p><h2>{t("mcpTokens.management.title")}</h2></div><span className="section-copy">{t("tokens.management.revokeHelp")}</span></div><div className="token-filter-bar"><TextInput label={t("tokens.filter.findLabel")} value={tokenSearch} onChange={setTokenSearch} placeholder={t("tokens.filter.findPlaceholder")}/><Selector label={t("common.status")} value={tokenFilter} onChange={setTokenFilter} options={[{value: "all", label: t("common.allStatuses")}, ...Object.entries(statusLabel).map(([value, label]) => ({value, label}))]}/></div>{actionError && <p className="inline-error" role="alert">{actionError}</p>}{accessLoading && !mcpTokens.length ? <p className="section-copy" role="status">{t("tokens.loadingTokens")}</p> : visibleTokens.length ? <div className="token-list">{visibleTokens.map(token => { const busy = mutatingTokenId.startsWith(`${token.id}:`) || mutatingTokenId === token.id; return <article className="token-row" key={token.id}><div><b>{token.name}</b><p>{t("tokens.tokenRow.summary", {prefix: token.token_prefix, toolCount: token.allowed_tools.length, kbCount: token.allowed_knowledge_base_ids.length})}</p><small>{t("tokens.tokenRow.limits", {rpm: token.requests_per_minute, concurrency: token.max_concurrent_requests, timeout: token.query_timeout_seconds})}{token.expires_at ? t("tokens.tokenRow.expires", {date: new Date(token.expires_at).toLocaleString()}) : ""}</small><details className="token-scope-details"><summary>{t("tokens.viewAccessScope")}</summary><div><b>{t("mcpTokens.scopeDetails.kbHeading")}</b><p>{token.allowed_knowledge_base_ids.map(id => kbNames[id] || id).join(", ") || t("common.none")}</p><b>{t("mcpTokens.scopeDetails.toolsHeading")}</b><p>{token.allowed_tools.map(tool => tool.replace(/_/g, " ")).join(", ") || t("common.none")}</p></div></details></div><StatusBadge status={token.status}/><div className="document-actions">{token.status !== "revoked" && <Button label={t("tokens.rotateKey")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId === token.id} isDisabled={Boolean(mutatingTokenId)} onClick={() => rotate(token)}/>} {token.status === "active" && <Button label={t("common.disable")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":disable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "disable")}/>} {token.status === "inactive" && <Button label={t("common.enable")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":enable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "enable")}/>} {token.status !== "revoked" && <Button label={t("tokens.revoke")} size="sm" variant="destructive" isLoading={busy && mutatingTokenId.endsWith(":revoke")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "revoke")}/>}</div></article>; })}</div> : <EmptyState title={mcpTokens.length ? t("mcpTokens.empty.noMatch") : t("mcpTokens.empty.none")} description={mcpTokens.length ? t("tokens.empty.tryAnother") : t("mcpTokens.empty.noneDescription")}/>}</section><section className="content-section"><h2>{t("tokens.audit.title")}</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || t("tokens.audit.systemFallback")} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title={t("tokens.audit.emptyTitle")} description={t("tokens.audit.emptyDescription")}/>}</section></>;
}

function IngestTokensView({selectedKb, knowledgeBases, tokens, auditLogs, loadAccess, createMcpToken, rotateMcpToken, changeTokenState}) {
  const {t} = useLanguage();
  const activeKnowledgeBases = knowledgeBases.filter(kb => kb.status === "active");
  const kbNames = useMemo(() => Object.fromEntries(knowledgeBases.map(kb => [kb.id, kb.name])), [knowledgeBases]);
  const ingestTokens = useMemo(() => tokens.filter(isIngestToken), [tokens]);
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
  const [ingestKbId, setIngestKbId] = useState(selectedKb ? selectedKb.id : "");
  const [snippetLanguage, setSnippetLanguage] = useState("curl");
  const [expiresAt, setExpiresAt] = useState("");
  const [rpm, setRpm] = useState(60);
  const [concurrency, setConcurrency] = useState(5);
  const [timeout, setTimeoutValue] = useState(60);
  const secretTimer = useRef(null);
  const ingestApiBase = `${window.location.origin}/api/v1`;
  // The secret is interpolated only while it is still on screen; otherwise the
  // snippet reads it from the environment so nothing durable holds it.
  const ingestSnippet = INGEST_SNIPPETS[snippetLanguage].build({apiBase: ingestApiBase, kbId: ingestKbId || "YOUR_KNOWLEDGE_BASE_ID", token: secret || "$SOFTNIX_INGEST_TOKEN"});
  const revealSecret = token => { if (secretTimer.current) window.clearTimeout(secretTimer.current); setSecret(token); secretTimer.current = window.setTimeout(() => setSecret(""), 120000); };
  const hideSecret = () => { if (secretTimer.current) window.clearTimeout(secretTimer.current); setSecret(""); };
  const copy = async (value, label) => { try { await navigator.clipboard.writeText(value); setCopyError(""); setCopied(label); window.setTimeout(() => setCopied(""), 1800); } catch { setCopyError(t("tokens.error.copyFailed")); } };
  const refreshAccess = async () => { setAccessLoading(true); setAccessLoadError(""); try { const result = await loadAccess(); if (result?.errors?.length) setAccessLoadError(result.errors.join(" · ")); } catch (error) { setAccessLoadError(error.message || t("tokens.error.loadAccessFailed")); } finally { setAccessLoading(false); } };
  const loadOperations = async () => { try { const [ready, projection] = await Promise.all([api("/v1/system/status"), api("/v1/system/graph-projection")]); setOperations({ready, projection}); setOperationsError(""); } catch (error) { setOperationsError(error.message || t("tokens.error.loadSystemStatusFailed")); } };
  useEffect(() => { refreshAccess(); loadOperations(); return () => { if (secretTimer.current) window.clearTimeout(secretTimer.current); }; }, []);
  useEffect(() => { const activeIds = new Set(activeKnowledgeBases.map(kb => kb.id)); setIngestKbId(current => activeIds.has(current) ? current : (selectedKb && activeIds.has(selectedKb.id) ? selectedKb.id : "")); }, [selectedKb, knowledgeBases]);
  const create = async event => { event.preventDefault(); setIsLoading(true); setFormError(""); try { const result = await createMcpToken({name, allowed_knowledge_base_ids: [], allowed_tools: [], allowed_scopes: [INGEST_SCOPE], allowed_ingest_knowledge_base_id: ingestKbId, expires_at: expiresAt ? new Date(expiresAt).toISOString() : null, requests_per_minute: Number(rpm), max_concurrent_requests: Number(concurrency), query_timeout_seconds: Number(timeout)}, "Ingest"); revealSecret(result.token); setName(""); } catch (error) { setFormError(error.message || t("ingestTokens.error.createFailed")); } finally { setIsLoading(false); } };
  const rotate = async token => { if (!window.confirm(t("tokens.confirm.rotate", {name: token.name}))) return; setMutatingTokenId(token.id); setActionError(""); try { const result = await rotateMcpToken(token.id, "Ingest"); revealSecret(result.token); await copy(result.token, "token"); } catch (error) { setActionError(error.message || t("tokens.error.rotateFailed")); } finally { setMutatingTokenId(""); } };
  const changeState = async (token, action) => { if (action === "revoke" && !window.confirm(t("ingestTokens.confirm.revoke", {name: token.name}))) return; if (action === "disable" && !window.confirm(t("ingestTokens.confirm.disable", {name: token.name}))) return; setMutatingTokenId(`${token.id}:${action}`); setActionError(""); try { await changeTokenState(token.id, action); } catch (error) { setActionError(error.message || t("tokens.error.changeStateFailed")); } finally { setMutatingTokenId(""); } };
  const visibleTokens = ingestTokens.filter(token => { const matchStatus = tokenFilter === "all" || token.status === tokenFilter; const needle = tokenSearch.trim().toLocaleLowerCase(); const matchSearch = !needle || `${token.name} ${token.token_prefix}`.toLocaleLowerCase().includes(needle); return matchStatus && matchSearch; });
  const statusLabel = {active: t("common.active"), inactive: t("common.inactive"), revoked: t("common.revoked")};
  return <><PageHeading eyebrow={t("ingestTokens.eyebrow")} title={t("ingestTokens.title")} description={t("ingestTokens.description")} actions={<Button label={t("tokens.refreshStatus")} variant="ghost" isLoading={accessLoading} onClick={() => { refreshAccess(); loadOperations(); }}/>}/>{(accessLoadError || operationsError || copyError) && <p className="inline-error access-error" role="alert">{accessLoadError || operationsError || copyError}</p>}<section className="mcp-overview"><div className="mcp-status"><span className="status-dot"/><div><b>{operationsError ? t("tokens.status.unavailable") : operations?.ready?.status || t("tokens.status.checking")}</b><span>{operations ? t("tokens.status.dependenciesOnline", {count: Object.keys(operations.ready.dependencies || {}).length}) : operationsError || t("tokens.status.loadingDependencies")}</span></div></div><div className="mcp-endpoint"><span>{t("ingestTokens.apiBaseLabel")}</span><code>{ingestApiBase}</code><button type="button" onClick={() => copy(ingestApiBase, "endpoint")}>{t("common.copy")}</button></div></section><section className="mcp-grid"><Card padding={4}><div className="card-heading"><div><p className="eyebrow">{t("tokens.step1Eyebrow")}</p><h2>{t("tokens.createScopedTokenTitle")}</h2></div><Badge label={t("tokens.secretShownOnce")} variant="warning"/></div><form className="form-stack" onSubmit={create}><TextInput label={t("tokens.form.nameLabel")} value={name} onChange={setName} placeholder={t("ingestTokens.form.namePlaceholder")} isRequired/><div className="scope-section ingest-scope"><div className="scope-heading"><b>{t("ingestTokens.form.writeAccessLabel")}</b><Badge label={t("ingestTokens.badgeIngestApi")} variant="warning"/></div><p className="section-copy">{t("ingestTokens.form.writeAccessHelp")}</p><div className="ingest-kb-picker"><p className="section-copy">{t("ingestTokens.form.kbPickerHelp")}</p><div className="scope-options">{activeKnowledgeBases.length ? activeKnowledgeBases.map(kb => <label key={kb.id} className={`scope-option ${ingestKbId === kb.id ? "selected" : ""}`}><input type="radio" name="ingest-kb" checked={ingestKbId === kb.id} onChange={() => setIngestKbId(kb.id)}/><span>{kb.name}</span></label>) : <p className="section-copy">{t("tokens.noActiveKb")}</p>}</div></div></div><details className="advanced-options"><summary>{t("tokens.form.advancedLimits")}</summary><div className="limit-grid"><label>{t("tokens.form.expiryLabel")}<input type="datetime-local" value={expiresAt} onChange={event => setExpiresAt(event.target.value)}/></label><label>{t("tokens.form.rpmLabel")}<input type="number" min="1" max="10000" value={rpm} onChange={event => setRpm(event.target.value)}/></label><label>{t("tokens.form.concurrencyLabel")}<input type="number" min="1" max="100" value={concurrency} onChange={event => setConcurrency(event.target.value)}/></label><label>{t("tokens.form.timeoutLabel")}<input type="number" min="1" max="300" value={timeout} onChange={event => setTimeoutValue(event.target.value)}/></label></div></details>{formError && <p className="inline-error" role="alert">{formError}</p>}<Button label={t("ingestTokens.form.submit")} type="submit" variant="primary" isLoading={isLoading} isDisabled={!name.trim() || !ingestKbId}/></form></Card><Card padding={4}><p className="eyebrow">{t("tokens.step2Eyebrow")}</p><h2>{t("ingestTokens.step2.title")}</h2><p className="section-copy">{t("ingestTokens.step2.description1Prefix")} <code>docs/INGEST_API.md</code></p><p className="section-copy">{t("ingestTokens.step2.uploadResponsePart1")} <code>202</code> {t("ingestTokens.step2.uploadResponsePart2")} <code>completed</code> {t("ingestTokens.step2.uploadResponsePart3")} <code>failed</code>{snippetLanguage === "curl" && secret ? "" : t("ingestTokens.step2.envHint")}</p><div className="ingest-language-tabs">{Object.entries(INGEST_SNIPPETS).map(([key, {label}]) => <button key={key} type="button" className={snippetLanguage === key ? "selected" : ""} onClick={() => setSnippetLanguage(key)}>{label}</button>)}</div><div className="code-panel"><div className="code-panel-top"><b>{INGEST_SNIPPETS[snippetLanguage].label}</b><button type="button" onClick={() => copy(ingestSnippet, "ingest snippet")}>{copied === "ingest snippet" ? t("common.copied") : t("ingestTokens.copySnippet")}</button></div><pre className="ingest-preview">{ingestSnippet}</pre></div>{secret && <div className="token-reveal"><b>{t("tokens.newTokenCopyNow")}</b><code>{secret}</code><div className="token-reveal-actions"><button type="button" onClick={() => copy(secret, "token")}>{copied === "token" ? t("common.copied") : t("tokens.copyToken")}</button><button type="button" onClick={hideSecret}>{t("tokens.hideToken")}</button></div></div>}</Card></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">{t("ingestTokens.apiReferenceEyebrow")}</p><h2>{t("ingestTokens.endpointsTitle")}</h2><p className="section-copy">{t("ingestTokens.apiRef.baseUrlLabel")} <code>{ingestApiBase}</code> {t("ingestTokens.apiRef.middle1")} <code>Authorization: Bearer &lt;token&gt;</code> {t("ingestTokens.apiRef.middle2")} <code>docs/INGEST_API.md</code></p></div></div><div className="ingest-endpoint-list">{INGEST_ENDPOINTS.map(endpoint => <article key={`${endpoint.method} ${endpoint.path}`} className="ingest-endpoint-row"><span className={`ingest-endpoint-method ingest-endpoint-method-${endpoint.method.toLowerCase()}`}>{endpoint.method}</span><div><code>{endpoint.path}</code><p>{t(endpoint.descriptionKey)}</p></div></article>)}</div></section><section className="content-section"><div className="section-title"><div><p className="eyebrow">{t("tokens.managementEyebrow")}</p><h2>{t("ingestTokens.management.title")}</h2></div><span className="section-copy">{t("tokens.management.revokeHelp")}</span></div><div className="token-filter-bar"><TextInput label={t("tokens.filter.findLabel")} value={tokenSearch} onChange={setTokenSearch} placeholder={t("tokens.filter.findPlaceholder")}/><Selector label={t("common.status")} value={tokenFilter} onChange={setTokenFilter} options={[{value: "all", label: t("common.allStatuses")}, ...Object.entries(statusLabel).map(([value, label]) => ({value, label}))]}/></div>{actionError && <p className="inline-error" role="alert">{actionError}</p>}{accessLoading && !ingestTokens.length ? <p className="section-copy" role="status">{t("tokens.loadingTokens")}</p> : visibleTokens.length ? <div className="token-list">{visibleTokens.map(token => { const busy = mutatingTokenId.startsWith(`${token.id}:`) || mutatingTokenId === token.id; return <article className="token-row" key={token.id}><div><b>{token.name}</b> <Badge label={t("ingestTokens.badgeIngest")} variant="warning"/><p>{token.token_prefix}… · {t("ingestTokens.tokenRow.writesToPrefix")} {kbNames[token.allowed_ingest_knowledge_base_id] || token.allowed_ingest_knowledge_base_id || t("ingestTokens.kbNotFound")}</p><small>{t("tokens.tokenRow.limits", {rpm: token.requests_per_minute, concurrency: token.max_concurrent_requests, timeout: token.query_timeout_seconds})}{token.expires_at ? t("tokens.tokenRow.expires", {date: new Date(token.expires_at).toLocaleString()}) : ""}</small></div><StatusBadge status={token.status}/><div className="document-actions">{token.status !== "revoked" && <Button label={t("tokens.rotateKey")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId === token.id} isDisabled={Boolean(mutatingTokenId)} onClick={() => rotate(token)}/>} {token.status === "active" && <Button label={t("common.disable")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":disable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "disable")}/>} {token.status === "inactive" && <Button label={t("common.enable")} size="sm" variant="secondary" isLoading={busy && mutatingTokenId.endsWith(":enable")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "enable")}/>} {token.status !== "revoked" && <Button label={t("tokens.revoke")} size="sm" variant="destructive" isLoading={busy && mutatingTokenId.endsWith(":revoke")} isDisabled={Boolean(mutatingTokenId)} onClick={() => changeState(token, "revoke")}/>}</div></article>; })}</div> : <EmptyState title={ingestTokens.length ? t("ingestTokens.empty.noMatch") : t("ingestTokens.empty.none")} description={ingestTokens.length ? t("tokens.empty.tryAnother") : t("ingestTokens.empty.noneDescription")}/>}</section><section className="content-section"><h2>{t("tokens.audit.title")}</h2>{auditLogs.length ? <div className="audit-list">{auditLogs.map(row => <div key={row.id}><span>{row.action}</span><small>{row.target_type || t("tokens.audit.systemFallback")} · {new Date(row.created_at).toLocaleString()}</small></div>)}</div> : <EmptyState isCompact title={t("tokens.audit.emptyTitle")} description={t("tokens.audit.emptyDescription")}/>}</section></>;
}


const Impact = ({data}) => {
  const {t} = useLanguage();
  return <div className="result-panel"><h3>{data.insufficient_evidence ? t("impact.insufficientEvidence") : t("impact.title", {name: data.subject.name})}</h3>{data.insufficient_evidence ? <p>{t("impact.insufficientEvidenceBody")}</p> : <><h4>{t("impact.direct")}</h4><ul>{data.direct_impacts.map(item => <li key={item.entity_id}>{item.name} <Badge label={item.relationship} variant="warning"/> {item.citation_ids.join(" ")}</li>)}</ul><h4>{t("impact.indirect")}</h4><ul>{data.indirect_impacts.map(item => <li key={item.entity_id}>{item.path.join(" → ")} {item.citation_ids.join(" ")}</li>)}</ul></>}</div>;
};
const Graph = ({data}) => {
  const {t} = useLanguage();
  return <div className="result-panel"><div className="graph-summary"><Badge label={t("graph.nodesCount", {count: data.nodes.length})} variant="info"/><Badge label={t("graph.connectionsCount", {count: data.edges.length})} variant="neutral"/></div><ul className="graph-list">{data.edges.map(edge => <li key={edge.id}><b>{data.nodes.find(node => node.id === edge.source)?.name}</b><span>{edge.type.replace(/_/g, " ")}</span><b>{data.nodes.find(node => node.id === edge.target)?.name}</b></li>)}</ul></div>;
};
const LEGAL_STATUS_VARIANTS = {in_force: "success", amended: "warning", not_yet_effective: "neutral", unknown: "neutral", superseded: "error", repealed: "error"};
const LegalStatusBadge = ({status}) => {
  const {language} = useLanguage();
  const labels = legalLabels[language];
  return status ? <Badge label={legalStatusLabel(labels, status)} variant={LEGAL_STATUS_VARIANTS[status] || "neutral"}/> : null;
};

const QueryResult = ({data, submitFeedback, onOpenSource}) => {
  const {t} = useLanguage();
  return <section className="query-result"><Card padding={4}><p className="eyebrow">{t("queryResult.answer")}</p><div className="answer-copy">{data.answer}</div>{data.warnings?.length > 0 && <div className="legal-warning-list" role="alert">{data.warnings.map((warning, index) => <p key={`${warning.code}-${index}`} className="inline-error">⚠ {warning.detail}</p>)}</div>}<div className="feedback-actions"><span>{t("queryResult.feedback.prompt")}</span><Button label={t("queryResult.feedback.yes")} size="sm" variant="secondary" onClick={() => submitFeedback(data.result_id, 1)}/><Button label={t("queryResult.feedback.no")} size="sm" variant="ghost" onClick={() => submitFeedback(data.result_id, -1)}/></div>{data.metadata?.retrieval_plan && <details className="retrieval-trace"><summary>{t("queryResult.trace.summary")}</summary><p>{data.metadata.retrieval_plan.intent} · {data.metadata.retrieval_plan.planner_source} · {(data.metadata.retrieval_plan.channels || []).join(", ") || t("queryResult.trace.noChannels")}{data.metadata.retrieval_plan.legal_context ? t("queryResult.trace.legalRegistry", {current: data.metadata.retrieval_plan.legal_context.current_version_ids?.length || 0, excluded: data.metadata.retrieval_plan.legal_context.excluded_document_ids?.length || 0}) : ""}</p><ul>{(data.metadata.retrieval_trace || []).map((step, index) => <li key={`${step.channel}-${index}`}><b>{step.system}</b><span>{step.status} · {step.result_count ?? 0} result(s) · {step.duration_ms ?? 0} ms</span></li>)}</ul></details>}</Card><div className="sources-heading"><h2>{t("queryResult.sources.heading")}</h2><p>{t("queryResult.sources.help")}</p></div><div className="source-grid">{data.sources.map(source => <Card key={source.citation_id} padding={3}><div className="source-card-heading"><Badge label={source.citation_id} variant="info"/><LegalStatusBadge status={source.document_status}/></div><h3>{source.title}</h3>{source.section_label && <p className="section-copy">{source.section_label}{source.version_label ? ` · ${source.version_label}` : ""}{source.effective_from ? t("queryResult.sources.effectiveFrom", {date: source.effective_from}) : ""}</p>}<p>{source.excerpt}</p><Button label={t("queryResult.sources.openSource")} size="sm" variant="ghost" onClick={() => onOpenSource({id: source.document_id, title: source.title})}/></Card>)}</div></section>;
};
function LegalInstrumentCard({instrument, onUpdate}) {
  const {t, language} = useLanguage();
  const labels = legalLabels[language];
  const [editing, setEditing] = useState(false);
  return <div className="legal-instrument-summary"><div className="legal-instrument-heading"><div><p className="eyebrow">{t("legal.card.eyebrow")}</p><h3>{instrument.official_title}</h3><p className="section-copy">{labels.kind[instrument.kind] || instrument.kind} · {t("legal.authorityLevel", {level: instrument.authority_level})}{instrument.version_label ? ` · ${instrument.version_label}` : ""}</p></div><LegalStatusBadge status={instrument.status}/></div>
    <dl className="legal-instrument-meta"><div><dt>{t("legal.override.effectiveFrom")}</dt><dd>{instrument.effective_from || "—"}</dd></div><div><dt>{t("legal.override.effectiveTo")}</dt><dd>{instrument.effective_to || "—"}</dd></div><div><dt>{t("legal.card.statusSourceLabel")}</dt><dd>{instrument.status_source}</dd></div></dl>
    {instrument.status_reason && <p className="section-copy">{instrument.status_reason}</p>}
    <Button label={editing ? t("common.cancel") : t("legal.card.overrideStatus")} variant="ghost" size="sm" onClick={() => setEditing(value => !value)}/>
    {editing && <LegalInstrumentOverrideForm row={instrument} onSave={payload => { onUpdate(instrument.id, payload); setEditing(false); }}/>}
  </div>;
}

function DocumentPreview({preview, jobs, isPollingJobs, pollingError, templates, legalInstrument, onExtractLegal, onSaveLegal, onDeleteLegal, onSaveDocumentMetadata, onUpdateLegalInstrument, onClose}) {
  const {t} = useLanguage();
  const [editingLegal, setEditingLegal] = useState(false);
  const [legalDraft, setLegalDraft] = useState("");
  const [legalError, setLegalError] = useState("");
  const [editingMetadata, setEditingMetadata] = useState(false);
  const [metadataDraft, setMetadataDraft] = useState({});
  const [tab, setTab] = useState("content");
  const headingRef = useRef(null);
  const modalRef = useRef(null);
  const hasLegalMetadata = Boolean(preview.legal_metadata && Object.keys(preview.legal_metadata).length);
  const hasActiveExtraction = jobs.some(job => job.type === "EXTRACT_LEGAL_METADATA" && isActiveProcessingJob(job));
  const isExtracting = hasActiveExtraction || isPollingJobs;
  useEffect(() => { setEditingLegal(false); setLegalError(""); setTab("content"); }, [preview.document_id]);
  useEffect(() => { if (!editingLegal) setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); }, [preview.legal_metadata, editingLegal]);
  useEffect(() => { setEditingMetadata(false); }, [preview.document_id]);
  useEffect(() => { if (!editingMetadata) setMetadataDraft(preview.document_metadata || {}); }, [preview.document_metadata, editingMetadata]);
  useEffect(() => {
    headingRef.current?.focus();
    const handleKeyDown = event => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...(modalRef.current?.querySelectorAll("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex=\"-1\"])") || [])]
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
  }, [preview.document_id, onClose]);
  const startEditing = () => { setLegalDraft(JSON.stringify(preview.legal_metadata || {articles: [], amendments: []}, null, 2)); setLegalError(""); setEditingLegal(true); };
  const save = async event => {
    event.preventDefault();
    try {
      const parsed = JSON.parse(legalDraft);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error(t("documentPreview.jsonObjectError"));
      await onSaveLegal({id: preview.document_id, title: preview.title}, parsed); setEditingLegal(false); setLegalError("");
    } catch (error) { setLegalError(error instanceof SyntaxError ? t("documentPreview.invalidJson") : error.message); }
  };
  const template = templates.find(row => row.id === preview.metadata_template_id);
  const documentFields = preview.metadata_template_fields?.length ? preview.metadata_template_fields : (template?.fields || []);
  const hasDocumentFields = Boolean(documentFields.length);
  const saveMetadata = async event => { event.preventDefault(); await onSaveDocumentMetadata({id: preview.document_id, title: preview.title}, metadataDraft); setEditingMetadata(false); };
  const tabs = [["content", t("documentPreview.tabs.content")], ["metadata", t("documentPreview.tabs.metadata")], ["legal", t("documentPreview.tabs.legal")], ["activity", t("documentPreview.tabs.activity")]];
  return <div className="document-preview-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={modalRef} className="document-preview-modal" role="dialog" aria-modal="true" aria-labelledby="document-preview-title" onMouseDown={event => event.stopPropagation()}>
      <div className="preview-heading"><div><p className="eyebrow">{t("documentPreview.eyebrow")}</p><h2 id="document-preview-title" tabIndex={-1} ref={headingRef}>{preview.title}</h2></div><div className="preview-actions"><StatusBadge status={preview.status}/>{isExtracting && <span className="live-status" role="status" aria-live="polite">{t("documentPreview.extractingMetadata")}</span>}{preview.status === "completed" && <Button label={isExtracting ? t("documentPreview.extractingMetadata") : t("documentPreview.extractLegalMetadata")} size="sm" variant="secondary" isLoading={isExtracting} isDisabled={isExtracting} onClick={() => onExtractLegal({id: preview.document_id, title: preview.title})}/>}<button type="button" className="drawer-close" onClick={onClose} aria-label={t("documentPreview.closeAriaLabel")}>×</button></div></div>
      {preview.error_code && <p className="inline-error">{preview.error_code}</p>}
      <nav className="document-preview-tabs" role="tablist" aria-label={t("documentPreview.sectionsAriaLabel")}>{tabs.map(([value, label]) => <button type="button" role="tab" aria-selected={tab === value} className={tab === value ? "selected" : ""} key={value} onClick={() => setTab(value)}>{label}</button>)}</nav>
      <div className="document-preview-tab-panel">
        {tab === "content" && <pre className="excerpt">{preview.text || t("documentPreview.content.placeholder")}</pre>}
        {tab === "metadata" && (hasDocumentFields ? <div className="document-metadata-panel"><div className="preview-heading"><div><h3>{preview.metadata_template_name || t("documentPreview.metadata.defaultTitle")}</h3><p className="section-copy">{t("documentPreview.metadata.helpText")}</p></div>{!editingMetadata && <Button label={t("documentPreview.metadata.editFields")} size="sm" variant="secondary" onClick={() => setEditingMetadata(true)}/>}</div>{editingMetadata ? <form onSubmit={saveMetadata}><MetadataFields fields={documentFields} values={metadataDraft} onChange={setMetadataDraft}/><div className="preview-actions"><Button label={t("documentPreview.metadata.saveFields")} type="submit" variant="primary"/><Button label={t("common.cancel")} type="button" variant="ghost" onClick={() => { setMetadataDraft(preview.document_metadata || {}); setEditingMetadata(false); }}/></div></form> : <dl className="document-metadata-values">{documentFields.filter(field => preview.document_metadata?.[field.key] !== undefined && preview.document_metadata?.[field.key] !== "").map(field => <div key={field.key}><dt>{field.label}</dt><dd>{String(preview.document_metadata[field.key])}</dd></div>)}</dl>}</div> : <p className="section-copy">{t("documentPreview.metadata.noFields")}</p>)}
        {tab === "legal" && <div className="legal-metadata-panel">{legalInstrument && <LegalInstrumentCard instrument={legalInstrument} onUpdate={onUpdateLegalInstrument}/>}<div className="legal-metadata-heading"><div><h3>{t("documentPreview.legal.heading")}</h3><p className="section-copy">{t("documentPreview.legal.description")}</p></div>{!editingLegal && <div className="legal-metadata-actions"><Button label={hasLegalMetadata ? t("documentPreview.legal.editMetadata") : t("documentPreview.legal.addMetadata")} size="sm" variant="secondary" onClick={startEditing}/>{hasLegalMetadata && <Button label={t("documentPreview.legal.deleteMetadata")} size="sm" variant="destructive" onClick={() => onDeleteLegal({id: preview.document_id, title: preview.title})}/>}</div>}</div>{editingLegal ? <form className="legal-editor" onSubmit={save}><textarea aria-label={t("documentPreview.legal.jsonAriaLabel")} value={legalDraft} onChange={event => setLegalDraft(event.target.value)} rows={18} spellCheck="false"/><p className="section-copy">{t("documentPreview.legal.helpPart1")} <code>instrument</code>, <code>provisions</code> {t("documentPreview.legal.helpPart2")} <code>references</code> {t("documentPreview.legal.helpPart3")} <code>evidence_quote</code>{t("documentPreview.legal.helpPart4")}</p>{legalError && <p className="inline-error" role="alert">{legalError}</p>}<div className="preview-actions"><Button label={t("documentPreview.legal.saveMetadata")} type="submit" variant="primary"/><Button label={t("common.cancel")} type="button" variant="ghost" onClick={() => setEditingLegal(false)}/></div></form> : hasLegalMetadata ? <pre className="excerpt legal-metadata">{JSON.stringify(preview.legal_metadata, null, 2)}</pre> : <p className="section-copy">{t("documentPreview.legal.emptyState", {addLabel: t("documentPreview.legal.addMetadata"), extractLabel: t("documentPreview.extractLegalMetadata")})}</p>}</div>}
        {tab === "activity" && <>{pollingError && <p className="inline-error" role="alert">{pollingError}</p>}{isPollingJobs && <p className="live-status" role="status" aria-live="polite">{t("documentPreview.activity.refreshing")}</p>}{jobs.length ? <div className="job-list">{jobs.map(job => <div key={job.id}><span>{job.type || t("documentPreview.activity.defaultType")} · {job.stage || t("documentPreview.activity.queued")}{job.attempt_count ? t("documentPreview.activity.attemptSuffix", {count: job.attempt_count}) : ""}{job.error_code ? ` · ${job.error_code}` : ""}{job.error_message ? `: ${job.error_message}` : ""}</span><StatusBadge status={job.status}/><span>{job.progress_percent}%</span></div>)}</div> : <p className="section-copy">{t("documentPreview.activity.noJobs")}</p>}</>}
      </div>
    </div>
  </div>;
}

function UsersView({users, groups, loadUsers, loadGroups, notify, showError}) {
  const {t} = useLanguage();
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({username: "", display_name: "", role: "user", group_id: "", password: ""});
  const [resetTarget, setResetTarget] = useState(null);
  const [resetPassword, setResetPassword] = useState("");
  const groupOptions = useMemo(() => [{value: "", label: t("users.noGroup")}, ...groups.map(group => ({value: group.id, label: group.name}))], [groups, t]);

  const openCreate = () => { setEditing(null); setForm({username: "", display_name: "", role: "user", group_id: "", password: ""}); setIsDrawerOpen(true); };
  const openEdit = user => { setEditing(user); setForm({username: user.username, display_name: user.display_name || "", role: user.role, group_id: user.group_id || "", password: ""}); setIsDrawerOpen(true); };

  const submit = async event => {
    event.preventDefault();
    try {
      const body = {display_name: form.display_name || null, role: form.role, group_id: form.group_id || null};
      if (editing) await api(`/v1/users/${editing.id}`, {method: "PATCH", body: JSON.stringify(body)});
      else await api("/v1/users", {method: "POST", body: JSON.stringify({...body, username: form.username, password: form.password})});
      notify(editing ? t("users.notify.updated") : t("users.notify.created"));
      setIsDrawerOpen(false);
      await Promise.all([loadUsers(), loadGroups()]);
    }
    catch (error) { showError(error); }
  };

  const submitReset = async event => {
    event.preventDefault();
    try {
      await api(`/v1/users/${resetTarget.id}/reset-password`, {method: "POST", body: JSON.stringify({password: resetPassword})});
      notify(t("users.notify.passwordReset"));
      setResetTarget(null); setResetPassword("");
    }
    catch (error) { showError(error); }
  };

  const toggleActive = async user => {
    try {
      await api(`/v1/users/${user.id}`, {method: "PATCH", body: JSON.stringify({is_active: !user.is_active})});
      await loadUsers();
      notify(t("users.notify.updated"));
    }
    catch (error) { showError(error); }
  };

  return <><PageHeading eyebrow={t("users.eyebrow")} title={t("users.title")} description={t("users.description")}/>
  <div className="users-toolbar"><Button label={t("users.create")} variant="primary" onClick={openCreate}/></div>
  <div className="table-scroll"><table className="data-table">
    <thead><tr><th>{t("users.table.username")}</th><th>{t("users.table.displayName")}</th><th>{t("users.table.role")}</th><th>{t("users.table.group")}</th><th>{t("users.table.status")}</th><th></th></tr></thead>
    <tbody>{users.map(user => <tr key={user.id}>
      <td><b>{user.username}</b></td>
      <td>{user.display_name || "—"}</td>
      <td><Badge label={t(`users.role.${user.role}`)} variant={user.role === "admin" ? "danger" : user.role === "manager" ? "warning" : "info"}/></td>
      <td>{groups.find(group => group.id === user.group_id)?.name || "—"}</td>
      <td><Badge label={user.is_active ? t("users.status.active") : t("users.status.inactive")} variant={user.is_active ? "success" : "neutral"}/></td>
      <td className="row-actions">
        <Button label={t("users.edit")} size="sm" variant="secondary" onClick={() => openEdit(user)}/>
        <Button label={t("users.resetPassword")} size="sm" variant="ghost" onClick={() => setResetTarget(user)}/>
        {user.is_active
          ? <Button label={t("users.deactivate")} size="sm" variant="ghost" onClick={() => toggleActive(user)}/>
          : <Button label={t("users.activate")} size="sm" variant="secondary" onClick={() => toggleActive(user)}/>}
      </td>
    </tr>)}</tbody>
  </table></div>
  {isDrawerOpen && <Drawer title={editing ? t("users.editTitle") : t("users.createTitle")} onClose={() => setIsDrawerOpen(false)}>
    <form className="stacked-form" onSubmit={submit}>
      {!editing && <TextInput label={t("users.table.username")} value={form.username} onChange={value => setForm({...form, username: value})} isRequired placeholder="somchai"/>}
      {!editing && <TextInput label={t("users.form.password")} type="password" value={form.password} onChange={value => setForm({...form, password: value})} isRequired placeholder="********"/>}
      <TextInput label={t("users.table.displayName")} value={form.display_name} onChange={value => setForm({...form, display_name: value})}/>
      <Selector label={t("users.table.role")} value={form.role} onChange={value => setForm({...form, role: value})} options={[{value: "user", label: t("users.role.user")}, {value: "manager", label: t("users.role.manager")}, {value: "admin", label: t("users.role.admin")}]}/>
      <Selector label={t("users.table.group")} value={form.group_id} onChange={value => setForm({...form, group_id: value})} options={groupOptions}/>
      <Button type="submit" label={editing ? t("users.save") : t("users.create")} variant="primary"/>
    </form>
  </Drawer>}
  {resetTarget && <Drawer title={t("users.resetPasswordTitle", {name: resetTarget.username})} onClose={() => { setResetTarget(null); setResetPassword(""); }}>
    <form className="stacked-form" onSubmit={submitReset}>
      <TextInput label={t("users.form.password")} type="password" value={resetPassword} onChange={setResetPassword} isRequired placeholder="********"/>
      <Button type="submit" label={t("users.resetPassword")} variant="primary"/>
    </form>
  </Drawer>}
  </>;
}


function GroupsView({groups, users, loadGroups, notify, showError}) {
  const {t} = useLanguage();
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState({name: "", description: ""});

  const openCreate = () => { setEditing(null); setForm({name: "", description: ""}); setIsDrawerOpen(true); };
  const openEdit = group => { setEditing(group); setForm({name: group.name, description: group.description || ""}); setIsDrawerOpen(true); };

  const submit = async event => {
    event.preventDefault();
    try {
      if (editing) await api(`/v1/groups/${editing.id}`, {method: "PATCH", body: JSON.stringify(form)});
      else await api("/v1/groups", {method: "POST", body: JSON.stringify(form)});
      notify(editing ? t("groups.notify.updated") : t("groups.notify.created"));
      setIsDrawerOpen(false);
      await loadGroups();
    }
    catch (error) { showError(error); }
  };

  const remove = async group => {
    if (!window.confirm(t("groups.confirm.delete", {name: group.name}))) return;
    try { await api(`/v1/groups/${group.id}`, {method: "DELETE"}); notify(t("groups.notify.deleted")); await loadGroups(); }
    catch (error) { showError(error); }
  };

  return <><PageHeading eyebrow={t("groups.eyebrow")} title={t("groups.title")} description={t("groups.description")}/>
  <div className="users-toolbar"><Button label={t("groups.create")} variant="primary" onClick={openCreate}/></div>
  <div className="table-scroll"><table className="data-table">
    <thead><tr><th>{t("groups.table.name")}</th><th>{t("groups.table.description")}</th><th>{t("groups.table.members")}</th><th></th></tr></thead>
    <tbody>{groups.map(group => <tr key={group.id}>
      <td><b>{group.name}</b></td>
      <td>{group.description || "—"}</td>
      <td>{users.filter(user => user.group_id === group.id).length}</td>
      <td className="row-actions">
        <Button label={t("groups.edit")} size="sm" variant="secondary" onClick={() => openEdit(group)}/>
        <Button label={t("groups.delete")} size="sm" variant="ghost" onClick={() => remove(group)}/>
      </td>
    </tr>)}</tbody>
  </table></div>
  {isDrawerOpen && <Drawer title={editing ? t("groups.editTitle") : t("groups.createTitle")} onClose={() => setIsDrawerOpen(false)}>
    <form className="stacked-form" onSubmit={submit}>
      <TextInput label={t("groups.table.name")} value={form.name} onChange={value => setForm({...form, name: value})} isRequired/>
      <TextInput label={t("groups.table.description")} value={form.description} onChange={value => setForm({...form, description: value})}/>
      <Button type="submit" label={editing ? t("groups.save") : t("groups.create")} variant="primary"/>
    </form>
  </Drawer>}
  </>;
}


function ProfileView({me, notify, showError}) {
  const {t} = useLanguage();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSaving, setIsSaving] = useState(false);

  const submit = async event => {
    event.preventDefault();
    if (newPassword !== confirmPassword) { notify(t("profile.error.passwordMismatch"), "error"); return; }
    setIsSaving(true);
    try {
      await api("/v1/auth/change-password", {method: "POST", body: JSON.stringify({current_password: currentPassword, new_password: newPassword})});
      notify(t("profile.notify.passwordChanged"));
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
    }
    catch (error) { showError(error); }
    finally { setIsSaving(false); }
  };

  return <><PageHeading eyebrow={t("profile.eyebrow")} title={t("profile.title")} description={t("profile.description")}/>
  <Card padding={4} variant="blue">
    <div className="profile-summary">
      <div><p className="eyebrow">{t("users.table.username")}</p><h3>{me?.username}</h3></div>
      <div><p className="eyebrow">{t("users.table.displayName")}</p><h3>{me?.display_name || "—"}</h3></div>
      <div><p className="eyebrow">{t("users.table.role")}</p><h3>{t(`users.role.${me?.role || "user"}`)}</h3></div>
      <div><p className="eyebrow">{t("users.table.group")}</p><h3>{me?.group?.name || "—"}</h3></div>
    </div>
  </Card>
  <Card padding={4}>
    <h2>{t("profile.changePassword")}</h2>
    <form className="stacked-form" onSubmit={submit}>
      <TextInput label={t("profile.currentPassword")} type="password" value={currentPassword} onChange={setCurrentPassword} isRequired/>
      <TextInput label={t("profile.newPassword")} type="password" value={newPassword} onChange={setNewPassword} isRequired/>
      <TextInput label={t("profile.confirmPassword")} type="password" value={confirmPassword} onChange={setConfirmPassword} isRequired/>
      <Button type="submit" label={t("profile.submit")} variant="primary" isLoading={isSaving} isDisabled={!currentPassword || !newPassword || isSaving}/>
    </form>
  </Card>
  </>;
}


createRoot(document.getElementById("root")).render(<LanguageProvider><App/></LanguageProvider>);
