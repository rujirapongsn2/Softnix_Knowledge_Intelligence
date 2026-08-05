import React, {createContext, useContext, useEffect, useMemo, useState} from "react";
import {translations} from "./translations.js";

const STORAGE_KEY = "softnix:language";
const detectDefaultLanguage = () => (navigator.language || "").toLowerCase().startsWith("th") ? "th" : "en";
const readStoredLanguage = () => {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "th" || stored === "en" ? stored : null;
};

const LanguageContext = createContext(null);

export function LanguageProvider({children}) {
  const [language, setLanguageState] = useState(() => readStoredLanguage() || detectDefaultLanguage());
  useEffect(() => { document.documentElement.lang = language; }, [language]);
  const setLanguage = next => { window.localStorage.setItem(STORAGE_KEY, next); setLanguageState(next); };
  const t = useMemo(() => (key, vars) => {
    const dict = translations[language] || translations.en;
    let value = dict[key] ?? translations.en[key] ?? key;
    if (vars) for (const [name, replacement] of Object.entries(vars)) value = value.replaceAll(`{${name}}`, String(replacement));
    return value;
  }, [language]);
  const value = useMemo(() => ({language, setLanguage, t}), [language, t]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export const useLanguage = () => useContext(LanguageContext);
