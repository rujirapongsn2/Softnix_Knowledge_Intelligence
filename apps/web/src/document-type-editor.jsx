import React, {useEffect, useRef, useState} from "react";
import {useLanguage} from "./language.jsx";
import {Button, DesignSystemCheckbox, Selector, TextArea, TextInput} from "./ui.jsx";
import {DOCUMENT_TYPE_OPTIONS} from "./document-type-options.js";
import {useDocumentTypeForm} from "./document-type-form.js";
import {fieldTypeHelp} from "./hints.mjs";

function DocumentTypeEditor({form, profileDefaults, onSubmit, onCancel}) {
  const {t} = useLanguage();
  const {draft, editing, error, fieldErrors, lastRemoved, changes} = form;
  // After "Add field", bring the new row into view and put the cursor in its key input.
  useEffect(() => {
    if (!form.focusRowId) return;
    const input = document.querySelector(`.template-form [data-row-id="${form.focusRowId}"] input`);
    if (!input) return;
    input.scrollIntoView({block: "center", behavior: "smooth"});
    input.focus({preventScroll: true});
    form.clearFocusRow();
  }, [form.focusRowId, draft.fields]);
  const fieldMessage = (scope, code) => code ? t(`documentType.fieldError.${scope}.${code}`) : undefined;
  const removedName = lastRemoved ? (lastRemoved.field.label || lastRemoved.field.key || t("documentType.removed.unnamed")) : "";
  const changeParts = [changes.added && t("documentType.unsaved.added", {count: changes.added}), changes.removed && t("documentType.unsaved.removed", {count: changes.removed}), changes.modified && t("documentType.unsaved.modified", {count: changes.modified}), changes.detailsChanged && t("documentType.unsaved.details")].filter(Boolean);
  return <form className="template-form" onSubmit={onSubmit} noValidate>
    <div className="drawer-form-heading"><div><p className="eyebrow">{editing ? t("documentType.editor.editEyebrow") : t("documentType.editor.newEyebrow")}</p><h3>{editing ? t("documentType.editor.editTitle") : t("documentType.editor.createTitle")}</h3></div><span className="section-copy">{t("documentType.editor.description")}</span></div>
    <TextInput label={t("documentType.editor.typeName")} value={draft.name} onChange={form.setName} placeholder={t("documentType.editor.typeNamePlaceholder")} error={fieldMessage("name", fieldErrors.name)} isRequired/>
    <TextInput label={t("documentType.editor.shortDescription")} value={draft.description} onChange={form.setDescription} placeholder={t("documentType.editor.shortDescriptionPlaceholder")} isOptional optionalLabel={t("common.optional")}/>
    <Selector label={t("documentType.editor.processingProfile")} value={draft.base_document_type} onChange={base => form.setProfile(base, profileDefaults)} options={DOCUMENT_TYPE_OPTIONS.map(option => ({value: option.value, label: t(option.labelKey)}))}/>
    <div className="template-field-builder"><div><b>{t("documentType.editor.metadataFields")} <span className="template-field-count">({t(draft.fields.length === 1 ? "documentType.drawer.fieldCountOne" : "documentType.drawer.fieldCountOther", {count: draft.fields.length})})</span></b><span className="template-field-actions"><Button label={t("documentType.editor.useProfileDefaults")} type="button" size="sm" variant="ghost" onClick={() => form.copyProfileDefaults(profileDefaults)} isDisabled={!profileDefaults[draft.base_document_type]?.length}/><Button label={t("documentType.editor.addField")} type="button" size="sm" variant="ghost" onClick={form.addField}/></span></div>{draft.fields.map((field, index) => { const fe = fieldErrors.fields[index] || {}; const rowId = field._uid || `metadata-field-${index}`; return <div className="template-field-row" key={rowId} data-row-id={field._uid}>
      <div className="template-field-section template-field-stored" role="group" aria-labelledby={`${rowId}-stored`}><h4 id={`${rowId}-stored`}>{t("documentType.editor.section.stored")}</h4>
        <div className="template-field-control"><TextInput label={t("documentType.editor.fieldKey")} value={field.key} onChange={key => form.patchField(index, {key})} placeholder="issuer" error={fieldMessage("key", fe.key)} isRequired/></div>
        <div className="template-field-control"><TextInput label={t("documentType.editor.label")} value={field.label} onChange={label => form.patchField(index, {label})} placeholder={t("documentType.editor.labelPlaceholder")} error={fieldMessage("label", fe.label)} isRequired/></div>
        <div className="template-field-control template-field-type"><Selector label={t("documentType.editor.fieldType")} value={field.field_type} onChange={field_type => form.patchField(index, {field_type})} description={fieldTypeHelp(t, field.field_type)} options={["text", "textarea", "text_list", "date", "number", "select", "multi_select", "boolean"].map(value => ({value, label: FIELD_TYPE_LABELS[value] ? t(FIELD_TYPE_LABELS[value]) : value}))}/></div>
        {["select", "multi_select"].includes(field.field_type) && <div className="template-field-control template-field-options"><TextInput label={t("documentType.editor.options")} value={field.options_text ?? (field.options || []).join(", ")} onChange={options_text => form.patchField(index, {options_text})} placeholder={t("documentType.editor.optionsPlaceholder")} description={t("documentType.editor.optionsHelp")} error={fieldMessage("options", fe.options)} isRequired/></div>}
      </div>
      <div className="template-field-section" role="group" aria-labelledby={`${rowId}-filling`}><h4 id={`${rowId}-filling`}>{t("documentType.editor.section.filling")}</h4>
        <div className="template-field-control"><Selector label={t("autoMetadata.fillMode")} value={field.fill_mode || "manual"} onChange={fill_mode => form.patchField(index, {fill_mode})} options={[{value: "extract", label: t("autoMetadata.extractMode")}, {value: "manual", label: t("autoMetadata.manualMode")}]}/></div>
        <div className="template-field-control template-field-required"><DesignSystemCheckbox label={t(field.fill_mode === "extract" ? "autoMetadata.requiredReview" : "documentType.editor.required")} checked={field.required} onChange={required => form.patchField(index, {required})}/></div>
        {field.fill_mode !== "extract" && <DesignSystemCheckbox label={t("autoMetadata.batchAllowed")} checked={Boolean(field.batch_default_allowed)} onChange={batch_default_allowed => form.patchField(index, {batch_default_allowed})}/>}
        {field.fill_mode === "extract" && <div className="template-field-control template-field-extraction"><TextArea label={t("autoMetadata.extractionInstruction")} value={field.extraction_description || ""} onChange={extraction_description => form.patchField(index, {extraction_description})} placeholder={t("autoMetadata.extractionInstructionPlaceholder")} description={t("autoMetadata.extractionInstructionDescription")} error={fieldMessage("extraction", fe.extraction)} isRequired/><DesignSystemCheckbox label={t("autoMetadata.alwaysReview")} checked={field.review_policy === "always"} onChange={checked => form.patchField(index, {review_policy: checked ? "always" : "evidence"})}/></div>}
        <div className="template-field-control template-field-help"><TextInput label={t("documentType.editor.helpText")} value={field.help_text || ""} onChange={help_text => form.patchField(index, {help_text})} placeholder={t("documentType.editor.helpTextPlaceholder")} isOptional optionalLabel={t("common.optional")}/></div>
      </div>
      <details className="template-field-advanced"><summary>{t("documentType.editor.capabilitiesSummary")}</summary><div className="template-field-capabilities"><DesignSystemCheckbox label={t("documentType.editor.searchCapability")} checked={field.searchable !== false} onChange={searchable => form.patchField(index, {searchable})}/><DesignSystemCheckbox label={t("documentType.editor.filterCapability")} checked={Boolean(field.filterable)} onChange={filterable => form.patchField(index, {filterable})}/><DesignSystemCheckbox label={t("documentType.editor.graphCapability")} checked={Boolean(field.graph_relationship)} onChange={enabled => form.patchField(index, enabled ? {graph_entity_type: field.graph_entity_type || "Entity", graph_relationship: field.graph_relationship || "RELATED_TO"} : {graph_entity_type: "", graph_relationship: ""})}/></div>{field.graph_relationship && <div className="template-field-control template-field-graph"><TextInput label={t("documentType.editor.graphEntityType")} value={field.graph_entity_type || ""} onChange={graph_entity_type => form.patchField(index, {graph_entity_type})} placeholder={t("documentType.editor.graphEntityTypePlaceholder")}/><TextInput label={t("documentType.editor.relationship")} value={field.graph_relationship || ""} onChange={graph_relationship => form.patchField(index, {graph_relationship: graph_relationship.toUpperCase().replace(/[^A-Z0-9_]/g, "")})} placeholder="ISSUED_BY"/></div>}</details>
      <div className="template-field-action"><Button label={t("documentType.editor.remove")} type="button" size="sm" variant="destructive" onClick={() => form.removeField(index)}/></div>
    </div>; })}</div>
    <div className="template-form-footer">
      {error && <p className="inline-error" role="alert">{error}</p>}
      {lastRemoved && <div className="template-field-notice" role="status"><span>{t("documentType.removed.notice", {name: removedName})}</span><Button label={t("documentType.removed.undo")} type="button" size="sm" variant="secondary" onClick={form.undoRemove}/></div>}
      {changes.total > 0 && <p className="template-unsaved" role="status"><b>{t("documentType.unsaved.title")}</b> {changeParts.join(" · ")} — {t("documentType.unsaved.hint")}</p>}
      <div className="preview-actions"><Button label={editing ? t("documentType.editor.save") : t("documentType.editor.createTitle")} type="submit" variant="primary"/><Button label={t("common.cancel")} type="button" variant="ghost" onClick={onCancel}/></div>
    </div>
  </form>;
}

const FIELD_TYPE_LABELS = {multi_select: "documentType.editor.fieldType.multiSelect", text_list: "documentType.editor.fieldType.textList"};

export function DocumentTypeDrawer({open, templates, onClose, onCreate, onUpdate, onDeactivate, onActivate, onRename, onDuplicate, onPurge}) {
  const {t} = useLanguage();
  const form = useDocumentTypeForm();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const requestCloseRef = useRef(onClose);
  const [renamingTemplate, setRenamingTemplate] = useState(null);
  const headingRef = useRef(null);
  const drawerRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    headingRef.current?.focus();
    const handleKeyDown = event => {
      if (event.key === "Escape") {
        requestCloseRef.current();
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
  }, [open]);
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
  const confirmDiscard = () => form.changes.total === 0 || window.confirm(t("documentType.unsaved.confirmDiscard"));
  const requestClose = () => { if (confirmDiscard()) onClose(); };
  requestCloseRef.current = requestClose;
  const cancelEditing = () => { if (confirmDiscard()) form.reset(); };
  const startCreate = () => { if (confirmDiscard()) form.startCreate(); };
  const startEdit = template => { if (confirmDiscard()) form.startEdit(template); };
  const submit = async event => {
    event.preventDefault();
    const count = form.validate();
    if (count) {
      form.setError(t("documentType.fieldError.summary", {count}));
      window.requestAnimationFrame(() => {
        const invalid = document.querySelector(".template-form .snx-field-invalid input, .template-form .snx-field-invalid textarea");
        invalid?.scrollIntoView({block: "center", behavior: "smooth"});
        invalid?.focus({preventScroll: true});
      });
      return;
    }
    try { const payload = form.toPayload(); if (form.editing) await onUpdate(form.editing.id, payload); else await onCreate(payload); form.reset(); }
    catch (requestError) { form.setError(requestError.message || t("documentType.drawer.error.saveFailed")); }
  };
  const confirmPurge = async template => {
    if (!window.confirm(t("documentType.drawer.confirmPurge", {name: template.name}))) return;
    const ok = await onPurge(template);
    if (ok) setRenamingTemplate(null);
  };
  const renderRow = template => <article className="document-type-row" key={template.id}>
    <div className="document-type-row-main"><div className="document-type-row-title"><b>{template.name}</b><span className={`template-status ${template.is_active === false ? "inactive" : "active"}`}>{template.is_active === false ? t("common.inactive") : t("common.active")}</span>{template.is_system && <span className="template-system-badge">{t("documentType.drawer.builtInBadge")}</span>}</div><p>{template.description || t("documentType.drawer.noDescription")}</p><small>{template.base_document_type} · {t(template.fields.length === 1 ? "documentType.drawer.fieldCountOne" : "documentType.drawer.fieldCountOther", {count: template.fields.length})} · {t(template.usage_count === 1 ? "documentType.drawer.usageCountOne" : "documentType.drawer.usageCountOther", {count: template.usage_count || 0})} · v{template.version}</small></div>
    {!template.is_system && <div className="document-type-row-actions">
      <Button label={t("common.edit")} size="sm" variant="secondary" onClick={() => startEdit(template)}/>
      <Button label={t("common.rename")} size="sm" variant="secondary" onClick={() => setRenamingTemplate(template)}/>
      <Button label={t("documentType.drawer.duplicate")} size="sm" variant="secondary" onClick={() => onDuplicate(template)}/>
      {template.is_active === false
        ? <Button label={t("common.restore")} size="sm" variant="secondary" onClick={() => onActivate(template)}/>
        : <Button label={t("documentType.drawer.archive")} size="sm" variant="secondary" onClick={() => onDeactivate(template)}/>}
      <Button label={t("common.delete")} size="sm" variant="destructive" onClick={() => confirmPurge(template)}/>
    </div>}
  </article>;
  return <div className="document-type-drawer-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) requestClose(); }}><aside ref={drawerRef} className="document-type-drawer" role="dialog" aria-modal="true" aria-labelledby="document-type-drawer-title" onMouseDown={event => event.stopPropagation()}>
    <header className="document-type-drawer-header"><div><p className="eyebrow">{t("documentType.drawer.eyebrow")}</p><h2 id="document-type-drawer-title" tabIndex={-1} ref={headingRef}>{t("documentType.drawer.title")}</h2><p>{t("documentType.drawer.countInKb", {count: templates.length})}</p></div><button type="button" className="drawer-close" onClick={requestClose} aria-label={t("documentType.drawer.close")}>×</button></header>
    <div className="document-type-controls"><TextInput label={t("documentType.drawer.findType")} value={search} onChange={setSearch} placeholder={t("documentType.drawer.findTypePlaceholder")}/><Selector label={t("common.status")} value={statusFilter} onChange={setStatusFilter} options={[{value: "all", label: t("common.allStatuses")}, {value: "active", label: t("common.active")}, {value: "inactive", label: t("common.inactive")}]}/></div>
    <div className="document-type-drawer-actions"><Button label={form.isOpen ? t("documentType.drawer.cancelEditing") : t("documentType.drawer.createType")} size="sm" variant="primary" onClick={() => form.isOpen ? cancelEditing() : startCreate()}/></div>
    {form.isOpen && <DocumentTypeEditor form={form} profileDefaults={profileDefaults} onSubmit={submit} onCancel={cancelEditing}/>}
    <section className="document-type-section"><div className="document-type-section-heading"><h3>{t("documentType.drawer.builtInTypes")}</h3><span>{systemTemplates.length}</span></div>{systemTemplates.length ? systemTemplates.map(renderRow) : <p className="document-type-empty">{t("documentType.drawer.noBuiltInMatch")}</p>}</section>
    <section className="document-type-section"><div className="document-type-section-heading"><h3>{t("documentType.drawer.customTypes")}</h3><span>{customTemplates.length}</span></div>{customTemplates.length ? customTemplates.map(renderRow) : <p className="document-type-empty">{t("documentType.drawer.noCustomTypes")}</p>}</section>
    {renamingTemplate && <RenameTemplateDialog template={renamingTemplate} onClose={() => setRenamingTemplate(null)} onSave={async (name, description) => { const updated = await onRename(renamingTemplate, name, description); if (updated) setRenamingTemplate(null); }} onPurge={confirmPurge}/>}
  </aside></div>;
}

function RenameTemplateDialog({template, onClose, onSave, onPurge}) {
  const {t} = useLanguage();
  const [name, setName] = useState(template.name);
  const [description, setDescription] = useState(template.description || "");
  const [isSaving, setIsSaving] = useState(false);
  const overlayRef = useRef(null);
  useEffect(() => {
    const handleKeyDown = event => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);
  const submit = async event => {
    event.preventDefault();
    if (!name.trim() || isSaving) return;
    setIsSaving(true);
    try { await onSave(name.trim(), description.trim()); }
    finally { setIsSaving(false); }
  };
  const confirmPurge = async () => {
    if (isSaving) return;
    const ok = await onPurge(template);
    if (ok) onClose();
  };
  return <div className="document-type-drawer-overlay" role="presentation" ref={overlayRef} onMouseDown={event => { if (event.target === overlayRef.current) onClose(); }}>
    <aside className="document-type-drawer rename-template-drawer" role="dialog" aria-modal="true" aria-labelledby="rename-template-title" onMouseDown={event => event.stopPropagation()}>
      <header className="document-type-drawer-header"><div><p className="eyebrow">{t("documentType.rename.eyebrow")}</p><h2 id="rename-template-title" tabIndex={-1}>{t("documentType.rename.title")}</h2><p>{t("documentType.rename.help", {code: template.code})}</p></div><button type="button" className="drawer-close" onClick={onClose} aria-label={t("documentType.drawer.close")}>×</button></header>
      <form className="stacked-form" onSubmit={submit}>
        <TextInput label={t("documentType.rename.nameLabel")} value={name} onChange={setName} placeholder={t("documentType.editor.typeNamePlaceholder")} isRequired hasAutoFocus/>
        <TextInput label={t("documentType.rename.descriptionLabel")} value={description} onChange={setDescription} placeholder={t("documentType.editor.shortDescriptionPlaceholder")} isOptional optionalLabel={t("common.optional")}/>
        <div className="preview-actions"><Button label={t("common.save")} type="submit" variant="primary" isLoading={isSaving} isDisabled={!name.trim() || (name.trim() === template.name && description.trim() === (template.description || ""))}/><Button label={t("common.cancel")} type="button" variant="ghost" onClick={onClose}/></div>
      </form>
      <div className="rename-template-danger">
        <b>{t("documentType.rename.dangerTitle")}</b>
        <p className="section-copy">{t("documentType.rename.dangerHelp")}</p>
        <Button label={t("documentType.rename.purge")} size="sm" variant="destructive" isDisabled={isSaving} onClick={confirmPurge}/>
      </div>
    </aside>
  </div>;
}
