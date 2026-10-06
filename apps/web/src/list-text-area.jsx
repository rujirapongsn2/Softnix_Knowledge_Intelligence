import React, {useEffect, useState} from "react";
import {TextArea} from "./ui.jsx";

const parse = text => text.split("\n").map(line => line.trim()).filter(Boolean);
const asList = value => Array.isArray(value) ? value : [];

// One item per line. The text typed so far is kept apart from the list so that a new empty line survives while typing.
export function ListTextArea({label, value, onChange, description, isDisabled}) {
  const [raw, setRaw] = useState(asList(value).join("\n"));
  useEffect(() => {
    if (JSON.stringify(parse(raw)) !== JSON.stringify(asList(value))) setRaw(asList(value).join("\n"));
  }, [value]);
  return <TextArea label={label} value={raw} rows={4} description={description} isDisabled={isDisabled} onChange={next => { setRaw(next); onChange(parse(next)); }}/>;
}
