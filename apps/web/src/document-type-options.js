export const DOCUMENT_TYPE_OPTIONS = [
  {value: "general", labelKey: "documentType.general.label", descriptionKey: "documentType.general.description"},
  {value: "legal", labelKey: "documentType.legal.label", descriptionKey: "documentType.legal.description"},
  {value: "regulation", labelKey: "documentType.regulation.label", descriptionKey: "documentType.regulation.description"},
  {value: "contract", labelKey: "documentType.contract.label", descriptionKey: "documentType.contract.description"},
];

export const documentTypeLabel = (t, type) => t(DOCUMENT_TYPE_OPTIONS.find(option => option.value === type)?.labelKey || "documentType.general.label");
export const documentTypeDescription = (t, type) => t(DOCUMENT_TYPE_OPTIONS.find(option => option.value === type)?.descriptionKey || "documentType.general.description");
