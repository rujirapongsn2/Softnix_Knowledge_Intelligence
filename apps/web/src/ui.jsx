import React, {forwardRef, useEffect, useState} from "react";
import {Button as KumoButton, Input as KumoInput, InputArea as KumoInputArea} from "@cloudflare/kumo";

const join = (...items) => items.filter(Boolean).join(" ");
const FOCUSABLE_SELECTOR = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

const focusableElements = container => [...(container?.querySelectorAll(FOCUSABLE_SELECTOR) || [])]
  .filter(element => element.getAttribute("aria-hidden") !== "true");

const trapFocus = (event, container) => {
  if (event.key !== "Tab") return;
  const focusable = focusableElements(container);
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

export function useDialogFocus({open, dialogRef, onClose, initialFocusRef}) {
  useEffect(() => {
    if (!open) return undefined;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => (initialFocusRef?.current || focusableElements(dialogRef.current)[0])?.focus());
    const onKeyDown = event => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      trapFocus(event, dialogRef.current);
    };
    const previousOverflow = document.body.style.overflow;
    document.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [open, dialogRef, initialFocusRef, onClose]);
}

export const Button = forwardRef(function Button({label, children, variant = "secondary", size = "md", isLoading = false, isDisabled = false, className, ...props}, ref) {
  const kumoVariant = variant === "destructive" ? "destructive" : variant === "ghost" ? "ghost" : variant;
  return <KumoButton ref={ref} variant={kumoVariant} loading={isLoading} disabled={isDisabled || isLoading} className={join("snx-button", `snx-button-${size}`, className)} {...props}>{children || label}</KumoButton>;
});

export function TextInput({label, value, onChange, description, isLabelHidden = false, isDisabled = false, isRequired = false, isOptional = false, optionalLabel, hasAutoFocus = false, className, ...props}) {
  const accessibleLabel = isOptional ? <>{label}{optionalLabel && <> <em>{optionalLabel}</em></>}</> : label;
  return <div className={join("snx-field", className)}>
    <KumoInput label={accessibleLabel} description={description} aria-label={props["aria-label"] || (isLabelHidden ? label : undefined)} value={value ?? ""} onChange={event => onChange?.(event.target.value)} disabled={isDisabled} required={isRequired} autoFocus={hasAutoFocus} {...props}/>
  </div>;
}

export function TextArea({label, value, onChange, description, isLabelHidden = false, isDisabled = false, isRequired = false, className, ...props}) {
  return <div className={join("snx-field", className)}>
    <KumoInputArea label={label} description={description} aria-label={props["aria-label"] || (isLabelHidden ? label : undefined)} value={value ?? ""} onChange={event => onChange?.(event.target.value)} disabled={isDisabled} required={isRequired} {...props}/>
  </div>;
}

export function Selector({label, value, onChange, options = [], description, isLabelHidden = false, isDisabled = false, className}) {
  const id = React.useId();
  return <label className={join("snx-field", className)} htmlFor={id}>
    {!isLabelHidden && <span className="snx-field-label">{label}</span>}
    <select id={id} className="snx-select" value={value} onChange={event => onChange?.(event.target.value)} disabled={isDisabled}>
      {options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
    {description && <small className="snx-field-description">{description}</small>}
  </label>;
}

export function CheckboxInput({label, value, checked, onChange, isDisabled = false, className}) {
  const resolvedValue = checked ?? value ?? false;
  return <label className={join("snx-checkbox", className)}><input type="checkbox" checked={resolvedValue} onChange={event => onChange?.(event.target.checked)} disabled={isDisabled}/><span>{label}</span></label>;
}

export function Badge({label, variant = "neutral", className}) {
  return <span className={join("snx-badge", `snx-badge-${variant}`, className)}>{label}</span>;
}

export function Card({children, padding = 3, variant = "default", className}) {
  return <section className={join("snx-card", `snx-card-pad-${padding}`, `snx-card-${variant}`, className)}>{children}</section>;
}

export function EmptyState({title, description, actions, isCompact = false}) {
  return <section className={join("snx-empty", isCompact && "snx-empty-compact")}><h2>{title}</h2>{description && <p>{description}</p>}{actions && <div>{actions}</div>}</section>;
}

export function FileInput({label, value = [], onChange, onRemove, description, isMultiple = false, maxFiles, accept, maxSize, isLoading = false, chooseLabel, uploadingLabel, tooManyFilesMessage, tooLargeFilesMessage, removeLabel = "Remove file"}) {
  const id = React.useId();
  const errorId = React.useId();
  const [selectionError, setSelectionError] = useState("");
  const files = Array.isArray(value) ? value : value ? [value] : [];
  return <div className="snx-file-input"><span className="snx-field-label">{label}</span><label htmlFor={id} className="snx-dropzone"><strong>{isLoading ? uploadingLabel : chooseLabel}</strong><span>{description}</span><input id={id} type="file" accept={accept} multiple={isMultiple} disabled={isLoading} aria-describedby={selectionError ? errorId : undefined} onChange={event => {
    const next = [...event.target.files];
    const tooMany = maxFiles && next.length > maxFiles;
    const tooLarge = maxSize ? next.filter(file => file.size > maxSize) : [];
    if (tooMany || tooLarge.length) {
      setSelectionError(tooMany ? tooManyFilesMessage : tooLargeFilesMessage?.(tooLarge));
      event.target.value = "";
      return;
    }
    setSelectionError("");
    onChange?.(next);
    event.target.value = "";
  }}/></label>{selectionError && <p id={errorId} className="snx-file-error" role="alert">{selectionError}</p>}{files.length > 0 && <ul className="snx-file-list">{files.map((file, index) => <li key={`${file.name}-${file.lastModified}`}><span>{file.name}<small>{Math.ceil(file.size / 1024)} KB</small></span>{onRemove && <button type="button" onClick={() => onRemove(index)} aria-label={`${removeLabel}: ${file.name}`}>×</button>}</li>)}</ul>}</div>;
}

export function ProgressBar({label, value, variant = "info", isIndeterminate = false}) {
  return <div className="snx-progress" role="progressbar" aria-label={label} aria-valuemin="0" aria-valuemax="100" aria-valuenow={isIndeterminate ? undefined : value}><span className={join(`snx-progress-${variant}`, isIndeterminate && "snx-progress-indeterminate")} style={isIndeterminate ? undefined : {width: `${Math.min(100, Math.max(0, value || 0))}%`}}/></div>;
}

export function Toast({body, type = "info", onDismiss, isAutoHide = false, autoHideDuration = 5000, dismissLabel}) {
  useEffect(() => {
    if (!isAutoHide) return undefined;
    const timer = window.setTimeout(onDismiss, autoHideDuration);
    return () => window.clearTimeout(timer);
  }, [isAutoHide, autoHideDuration, onDismiss]);
  return <div className={join("snx-toast", `snx-toast-${type}`)} role={type === "error" ? "alert" : "status"}><span>{body}</span><button type="button" onClick={onDismiss} aria-label={dismissLabel}>×</button></div>;
}

export function SideNav({header, topContent, children, collapsible = false, mobileOpen = false, ariaLabel, expandLabel, collapseLabel}) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("softnix:rail-collapsed") === "1");
  const toggle = () => { const next = !collapsed; setCollapsed(next); localStorage.setItem("softnix:rail-collapsed", next ? "1" : "0"); };
  return <aside className={join("snx-rail", collapsed && "snx-rail-collapsed", mobileOpen && "snx-rail-mobile-open")} aria-label={ariaLabel}>
    <div className="snx-rail-brand">{header}{collapsible && <button type="button" className="snx-icon-button snx-rail-toggle" onClick={toggle} aria-label={collapsed ? expandLabel : collapseLabel}>{collapsed ? "›" : "‹"}</button>}</div>
    <div className="snx-rail-create">{topContent}</div><nav className="snx-rail-nav">{children}</nav>
  </aside>;
}

export function SideNavHeading({superheading, heading}) { return <div className="snx-brand-heading"><span>{superheading}</span><strong>{heading}</strong></div>; }
export function SideNavSection({title, subtitle, children, className}) { return <section className={join("snx-nav-section", className)}><p>{title}</p>{subtitle && <small>{subtitle}</small>}<div>{children}</div></section>; }
export function SideNavItem({label, isSelected, onClick, icon}) { return <button type="button" className={join("snx-nav-item", isSelected && "is-active")} onClick={() => { onClick?.(); window.dispatchEvent(new Event("softnix:navigate")); }} title={label}>{icon}<span>{label}</span></button>; }

export function TopNav({heading, endContent, onMenu, onCommand, label, menuLabel, commandLabel, isMenuOpen = false}) { return <header className="snx-topbar" aria-label={label}><button type="button" className="snx-icon-button snx-mobile-menu" onClick={onMenu} aria-label={menuLabel} aria-expanded={isMenuOpen}>☰</button><div className="snx-topbar-heading">{heading}</div><div className="snx-topbar-end"><button type="button" className="snx-command-trigger" onClick={onCommand} aria-label={commandLabel}>⌘K</button>{endContent}</div></header>; }
export function TopNavHeading({heading}) { return <strong>{heading}</strong>; }

export function AppShell({sideNav, topNav, onCommand, children, closeNavigationLabel}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const mobileDrawerRef = React.useRef(null);
  const closeMobile = React.useCallback(() => setMobileOpen(false), []);
  useDialogFocus({open: mobileOpen, dialogRef: mobileDrawerRef, onClose: closeMobile});
  useEffect(() => {
    window.addEventListener("softnix:navigate", closeMobile);
    return () => window.removeEventListener("softnix:navigate", closeMobile);
  }, []);
  return <div className="snx-shell"><div className="snx-rail-desktop">{sideNav}</div>{mobileOpen && <><button type="button" className="snx-drawer-scrim" aria-label={closeNavigationLabel} onClick={closeMobile}/><div ref={mobileDrawerRef} className="snx-rail-mobile" role="dialog" aria-modal="true" aria-label={sideNav.props.ariaLabel}>{React.cloneElement(sideNav, {mobileOpen: true})}</div></>}<div className="snx-main"><div>{React.cloneElement(topNav, {onMenu: () => setMobileOpen(true), onCommand, isMenuOpen: mobileOpen})}</div><main className="snx-content">{children}</main></div></div>;
}

export function CommandPalette({open, onClose, items = [], title, searchPlaceholder, searchLabel, noMatchLabel}) {
  const [query, setQuery] = useState("");
  const inputRef = React.useRef(null);
  const paletteRef = React.useRef(null);
  useEffect(() => { if (open) setQuery(""); }, [open]);
  useDialogFocus({open, dialogRef: paletteRef, initialFocusRef: inputRef, onClose});
  if (!open) return null;
  const needle = query.trim().toLocaleLowerCase();
  const visible = items.filter(item => !needle || `${item.label} ${item.group || ""}`.toLocaleLowerCase().includes(needle));
  return <div className="snx-command-overlay" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={paletteRef} className="snx-command-palette" role="dialog" aria-modal="true" aria-label={title}><div><input ref={inputRef} value={query} onChange={event => setQuery(event.target.value)} placeholder={searchPlaceholder} aria-label={searchLabel}/><kbd>Esc</kbd></div><ul>{visible.map(item => <li key={item.id}><button type="button" onClick={() => { item.onSelect(); onClose(); }}><span>{item.icon}</span><strong>{item.label}</strong>{item.group && <small>{item.group}</small>}</button></li>)}{!visible.length && <li className="snx-command-empty">{noMatchLabel}</li>}</ul></section></div>;
}

export function Theme({children}) { return children; }
