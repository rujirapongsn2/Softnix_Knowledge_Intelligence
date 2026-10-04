import {useCallback, useEffect, useLayoutEffect, useRef, useState} from "react";

const GAP = 4;
const VIEWPORT_MARGIN = 8;

// A dropdown that is placed against the viewport (so no clipping ancestor can hide it) and opens
// upward when it does not fit below its trigger. The menu is measured, not assumed to have a height.
export function useAnchoredMenu() {
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState(null);
  const rootRef = useRef(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const close = useCallback(() => setOpen(false), []);

  useLayoutEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current.getBoundingClientRect();
    const menu = menuRef.current.getBoundingClientRect();
    const right = Math.max(VIEWPORT_MARGIN, window.innerWidth - trigger.right);
    const fitsBelow = window.innerHeight - trigger.bottom >= menu.height + VIEWPORT_MARGIN;
    setStyle(fitsBelow ? {top: trigger.bottom + GAP, right} : {bottom: window.innerHeight - trigger.top + GAP, right});
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOutside = event => { if (!rootRef.current?.contains(event.target)) close(); };
    const closeOnEscape = event => { if (event.key === "Escape") close(); };
    const closeOnPageScroll = event => { if (!menuRef.current?.contains(event.target)) close(); };
    document.addEventListener("mousedown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("scroll", closeOnPageScroll, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("scroll", closeOnPageScroll, true);
      window.removeEventListener("resize", close);
    };
  }, [open, close]);

  return {open, style, rootRef, triggerRef, menuRef, close, toggle: () => setOpen(value => !value)};
}
