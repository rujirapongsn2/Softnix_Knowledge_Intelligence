"""Compare the baseline and candidate extractions in run_eval.py's results.jsonl: per-field agreement, statuses,
legal output and cost. python3 analyze.py results.jsonl; the cases behind each count go to results-examples.json."""
import json
import re
import sys
from collections import Counter, defaultdict

rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
meta = defaultdict(dict)
legal = defaultdict(dict)
for row in rows:
    if row["kind"] == "metadata":
        meta[row["document_id"]][row["model"]] = row
    elif row["kind"] == "legal":
        legal[row["document_id"]][row["model"]] = row
usage = next((r["usage"] for r in rows if r["kind"] == "usage"), {})


def norm(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    text = text.replace("ํา", "ำ").strip().strip("\"'“”").lower()
    return re.sub(r"\s+", " ", text)


def values(observation):
    return [norm(c["value"]) for c in (observation or {}).get("candidates", [])]


def compare(baseline, candidate, field_type):
    a, b = values(baseline), values(candidate)
    if not a and not b:
        return "both_empty"
    if not b:
        return "baseline_only"
    if not a:
        return "candidate_only"
    if field_type == "text_list":
        overlap = len(set(a) & set(b)) / max(1, min(len(set(a)), len(set(b))))
        return "same" if set(a) == set(b) else "overlap" if overlap >= 0.5 else "differ"
    if a[0] == b[0]:
        return "same"
    if set(a) & set(b):
        return "overlap"
    if a[0] in b[0] or b[0] in a[0]:
        return "overlap"
    return "differ"


outcomes, by_field, statuses, accepted_same = Counter(), defaultdict(Counter), {"baseline": Counter(), "candidate": Counter()}, Counter()
errors, seconds, examples = Counter(), defaultdict(list), defaultdict(list)
for doc_id, pair in meta.items():
    if set(pair) != {"baseline", "candidate"}:
        continue
    for name in pair:
        seconds[name].append(pair[name]["seconds"])
        if pair[name]["error"]:
            errors[(name, pair[name]["error"])] += 1
    if pair["baseline"]["error"] or pair["candidate"]["error"]:
        continue
    for key, field_type in pair["baseline"]["fields"].items():
        s, l = pair["baseline"]["observations"].get(key), pair["candidate"]["observations"].get(key)
        for name, obs in (("baseline", s), ("candidate", l)):
            statuses[name][(obs or {}).get("status", "missing")] += 1
        outcome = compare(s, l, field_type)
        outcomes[outcome] += 1
        by_field[key][outcome] += 1
        if (s or {}).get("status") == "accepted" and (l or {}).get("status") == "accepted":
            accepted_same["both_accepted_" + ("same" if outcome == "same" else "different")] += 1
        if outcome in ("differ", "candidate_only", "baseline_only", "overlap"):
            examples[outcome].append({"document_id": doc_id, "field": key,
                                      "baseline": [(c["value"], c["evidence"]["quote"][:160]) for c in (s or {}).get("candidates", [])[:2]],
                                      "candidate": [(c["value"], c["evidence"]["quote"][:160]) for c in (l or {}).get("candidates", [])[:2]],
                                      "status": [(s or {}).get("status"), (l or {}).get("status")]})

legal_fields = Counter()
legal_examples = []
for doc_id, pair in legal.items():
    if set(pair) != {"baseline", "candidate"}:
        continue
    for name in pair:
        if pair[name]["error"]:
            errors[(name, "legal:" + pair[name]["error"])] += 1
    s, l = pair["baseline"]["result"] or {}, pair["candidate"]["result"] or {}
    si, li = s.get("instrument") or {}, l.get("instrument") or {}
    for key in sorted(set(si) | set(li)):
        if key in ("evidence_quote", "summary"):
            continue
        legal_fields[(key, "same" if norm(si.get(key)) == norm(li.get(key)) else "differ")] += 1
    srel = {(r.get("relationship"), norm(r.get("target_title"))) for r in s.get("relationships") or []}
    lrel = {(r.get("relationship"), norm(r.get("target_title"))) for r in l.get("relationships") or []}
    legal_fields[("relationships", "same" if srel == lrel else "overlap" if srel & lrel else "differ" if srel or lrel else "both_empty")] += 1
    legal_fields[("provisions_count", "same" if len(s.get("provisions") or []) == len(l.get("provisions") or []) else "differ")] += 1
    legal_examples.append({"document_id": doc_id, "baseline": {k: si.get(k) for k in si if k != "evidence_quote"}, "candidate": {k: li.get(k) for k in li if k != "evidence_quote"},
                           "relationships": [sorted(srel), sorted(lrel)], "provisions": [len(s.get("provisions") or []), len(l.get("provisions") or [])]})

compared = sum(outcomes.values())
summary = {
    "documents": len(meta), "legal_documents": len(legal), "fields_compared": compared,
    "outcomes": dict(outcomes.most_common()),
    "statuses": {name: dict(counter.most_common()) for name, counter in statuses.items()},
    "both_accepted": dict(accepted_same),
    "weak_fields": {key: dict(c) for key, c in sorted(by_field.items(), key=lambda kv: -(kv[1]["differ"] + kv[1]["baseline_only"] + kv[1]["candidate_only"]))[:10]},
    "legal": {f"{k}:{v}": n for (k, v), n in sorted(legal_fields.items())},
    "errors": {f"{m}:{e}": n for (m, e), n in errors.items()},
    "seconds_per_document": {name: round(sum(v) / len(v), 1) for name, v in seconds.items() if v},
    "usage": usage,
}
print(json.dumps(summary, ensure_ascii=False, indent=1))
json.dump({"examples": examples, "legal_examples": legal_examples}, open(sys.argv[1].replace(".jsonl", "-examples.json"), "w"), ensure_ascii=False, indent=1)
