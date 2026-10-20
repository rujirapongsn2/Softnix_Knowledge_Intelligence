import React, {useId, useLayoutEffect, useRef, useState} from "react";
import {createPortal} from "react-dom";

const WIDTH = 360;
const MARGIN = 8;

// Below the anchor unless the card does not fit there and the space above is larger.
function placement(rect, height) {
  const left = Math.max(MARGIN, Math.min(rect.left, window.innerWidth - WIDTH - MARGIN));
  const below = window.innerHeight - rect.bottom;
  return rect.bottom + 6 + height > window.innerHeight && rect.top > below ? {left, bottom: window.innerHeight - rect.top + 6} : {left, top: rect.bottom + 6};
}

function HintSection({section}) {
  const List = section.ordered ? "ol" : "ul";
  return <div className={`hint-section hint-section-${section.kind}`}>
    <p className="hint-heading">{section.heading}</p>
    <List>{section.items.map(item => <li key={item}>{item}</li>)}</List>
  </div>;
}

export function HintCard({tip}) {
  return <>
    {tip.title && <p className="hint-title">{tip.title}</p>}
    <p>{tip.meaning}</p>
    {tip.sections?.map(section => <HintSection key={section.kind} section={section}/>)}
    {tip.action && <p className="hint-action">{tip.action}</p>}
    {tip.detail && <code className="hint-detail">{tip.detail}</code>}
  </>;
}

// Shows `tip` on hover and keyboard focus. The card is portaled so no table or drawer can clip it.
export function Hint({tip, focusable = false, className, children}) {
  const id = useId();
  const [anchor, setAnchor] = useState(null);
  const [height, setHeight] = useState(0);
  const card = useRef(null);
  // Measured before paint, so a tall card opens on the side it fits without a visible jump.
  useLayoutEffect(() => { if (anchor && card.current) setHeight(card.current.offsetHeight); }, [anchor]);
  if (!tip) return children;
  const position = anchor && placement(anchor, height);
  const show = event => setAnchor(event.currentTarget.getBoundingClientRect());
  const hide = () => { setAnchor(null); setHeight(0); };
  return <span className={["hint-anchor", className].filter(Boolean).join(" ")} tabIndex={focusable ? 0 : undefined} aria-describedby={position ? id : undefined}
    onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide} onKeyDown={event => event.key === "Escape" && hide()}>
    {children}
    {position && createPortal(<div ref={card} id={id} role="tooltip" className="hint-card" style={{...position, width: WIDTH}}><HintCard tip={tip}/></div>, document.body)}
  </span>;
}
