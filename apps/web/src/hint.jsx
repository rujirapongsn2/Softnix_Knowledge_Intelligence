import React, {useId, useState} from "react";
import {createPortal} from "react-dom";

const WIDTH = 320;
const MARGIN = 8;

function placement(rect) {
  const left = Math.max(MARGIN, Math.min(rect.left, window.innerWidth - WIDTH - MARGIN));
  return rect.bottom + 180 > window.innerHeight ? {left, bottom: window.innerHeight - rect.top + 6} : {left, top: rect.bottom + 6};
}

export function HintCard({tip}) {
  return <>
    <p>{tip.meaning}</p>
    {tip.pending && <p className="hint-pending">{tip.pending}</p>}
    {tip.action && <p className="hint-action">{tip.action}</p>}
    {tip.detail && <code className="hint-detail">{tip.detail}</code>}
  </>;
}

// Shows `tip` on hover and keyboard focus. The card is portaled so no table or drawer can clip it.
export function Hint({tip, focusable = false, className, children}) {
  const id = useId();
  const [position, setPosition] = useState(null);
  if (!tip) return children;
  const show = event => setPosition(placement(event.currentTarget.getBoundingClientRect()));
  const hide = () => setPosition(null);
  return <span className={["hint-anchor", className].filter(Boolean).join(" ")} tabIndex={focusable ? 0 : undefined} aria-describedby={position ? id : undefined}
    onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onKeyDown={event => event.key === "Escape" && hide()}>
    {children}
    {position && createPortal(<div id={id} role="tooltip" className="hint-card" style={{...position, width: WIDTH}}><HintCard tip={tip}/></div>, document.body)}
  </span>;
}
