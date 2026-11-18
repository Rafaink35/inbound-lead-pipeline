"""Lead pipeline demo: capture -> dedupe -> enrich -> score (Claude) -> route -> draft follow-up.

All data is fictional. Nothing is ever sent automatically: follow-ups are drafts
for a human to approve.
"""
import csv, json, os, re, sys, time, hashlib
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
FREE_DOMAINS = {"gmail.example", "hotmail.example", "yahoo.example"}
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def log(event, **data):
    with open(OUT / "run_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": round(time.time(), 1), "event": event, **data}) + "\n")


# 1. capture + dedupe -------------------------------------------------------
def load_leads():
    with open(ROOT / "data/leads_sample.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dedupe(leads):
    seen, unique = {}, []
    for lead in leads:
        key = hashlib.sha1(lead["email"].strip().lower().encode()).hexdigest()
        if key in seen:
            seen[key]["message"] += " | (follow-up) " + lead["message"]
            log("duplicate_merged", email=lead["email"], dropped_id=lead["id"])
            continue
        seen[key] = lead
        unique.append(lead)
    return unique


# 2. enrich -----------------------------------------------------------------
def enrich(lead):
    domain = lead["email"].split("@")[-1].lower()
    db = json.loads((ROOT / "data/company_db.json").read_text())
    info = db.get(domain, {})
    lead["domain"] = domain
    lead["free_email"] = domain in FREE_DOMAINS
    lead["industry"] = info.get("industry", "unknown")
    lead["employees"] = info.get("employees")
    lead["country"] = info.get("country", "unknown")
    return lead


# 3. score ------------------------------------------------------------------
PROMPT = """You qualify inbound B2B leads against an ideal customer profile.

ICP:
{icp}

Lead:
{lead}

Return ONLY valid JSON with exactly these keys:
{{"score": int 0-100, "tier": "hot"|"warm"|"cold", "reasons": [up to 3 strings, each under 12 words],
 "summary": one sentence for the CRM, "next_step": one short action}}
Base the score only on the information above. If something is unknown, say so; do not guess."""


def build_prompt(lead):
    icp = (ROOT / "icp.json").read_text()
    shown = {k: lead[k] for k in ("name", "company", "source", "message", "industry", "employees", "free_email")}
    return PROMPT.format(icp=icp, lead=json.dumps(shown, ensure_ascii=False))


def claude_score(lead):
    import anthropic  # only needed when a key is set
    client = anthropic.Anthropic()
    msg = client.messages.create(model=MODEL, max_tokens=400,
                                 messages=[{"role": "user", "content": build_prompt(lead)}])
    # The reply can start with a non-text block (e.g. reasoning); keep only text blocks.
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(re.search(r"\{.*\}", text, re.S).group(0))


def heuristic_score(lead):
    """Offline fallback with the same output schema. Also used when the model fails."""
    s, reasons = 30, []
    text = lead["message"].lower()
    if lead["free_email"] or not lead["company"]:
        s -= 20; reasons.append("free email or no company")
    if "buy cheap" in text or "click here" in text:
        s = 0; reasons = ["looks like spam"]
    emp = lead["employees"]
    if emp and emp >= 50: s += 25; reasons.append(f"{emp} employees")
    elif emp and emp < 10: s -= 10; reasons.append("very small company")
    if lead["industry"] in ("Logistics", "Healthcare", "Retail", "Technology", "Food manufacturing"):
        s += 10; reasons.append(f"fits industry: {lead['industry']}")
    for kw, pts, why in [("budget", 15, "budget mentioned"), ("next month", 12, "has a deadline"),
                         ("this quarter", 10, "evaluating this quarter"), ("proposal", 12, "asked for a proposal"),
                         ("demo", 8, "asked for a demo"), ("head of", 8, "decision-maker title")]:
        if kw in text: s += pts; reasons.append(why)
    s = max(0, min(100, s))
    tier = "hot" if s >= 75 else "warm" if s >= 45 else "cold"
    return {"score": s, "tier": tier, "reasons": reasons[:3] or ["not enough information"],
            "summary": f"{lead['name']} ({lead['company'] or 'no company'}): scored {s} by rules.",
            "next_step": {"hot": "Call within 1 business day", "warm": "Add to nurture sequence", "cold": "No outreach"}[tier]}


def valid(r):
    return (isinstance(r, dict) and isinstance(r.get("score"), int) and 0 <= r["score"] <= 100
            and r.get("tier") in ("hot", "warm", "cold") and isinstance(r.get("reasons"), list)
            and r.get("summary") and r.get("next_step"))


def score(lead):
    if os.environ.get("ANTHROPIC_API_KEY"):
        for attempt in (1, 2):
            try:
                r = claude_score(lead)
                if valid(r):
                    r["scorer"] = "claude"
                    return r
                log("invalid_model_output", id=lead["id"], attempt=attempt, output=str(r)[:300])
            except Exception as e:  # network, rate limit, bad JSON
                log("model_error", id=lead["id"], attempt=attempt, error=str(e)[:120])
    r = heuristic_score(lead)
    r["scorer"] = "rules_fallback"
    return r


# 4. route + draft ------------------------------------------------------------
def draft_followup(lead, r):
    if r["tier"] == "cold":
        return None
    opener = ("Thanks for reaching out" if r["tier"] == "hot" else "Thanks for your interest")
    ask = "Could we book 20 minutes this week?" if r["tier"] == "hot" else "I'll send a short guide that matches your case."
    return (f"Subject: Re: your request\n\nHi {lead['name'].split()[0]},\n\n{opener}. "
            f"You mentioned: \"{lead['message'][:90].rstrip()}...\" That is something we can help with. {ask}\n\n[DRAFT - needs human approval before sending]\n")


def main():
    OUT.mkdir(exist_ok=True)
    (OUT / "followups").mkdir(exist_ok=True)
    for p in list(OUT.glob("*")) + list((OUT / "followups").glob("*")):
        if p.is_file(): p.unlink()
    raw = load_leads()
    leads = dedupe(raw)
    rows = []
    for lead in leads:
        enrich(lead)
        r = score(lead)
        d = draft_followup(lead, r)
        if d:
            (OUT / "followups" / f"lead_{lead['id']}.txt").write_text(d, encoding="utf-8")
        rows.append({"id": lead["id"], "name": lead["name"], "company": lead["company"], "industry": lead["industry"],
                     "employees": lead["employees"], "score": r["score"], "tier": r["tier"],
                     "owner_alert": "YES" if r["tier"] == "hot" else "",
                     "reasons": "; ".join(r["reasons"]), "summary": r["summary"], "next_step": r["next_step"],
                     "scorer": r["scorer"]})
        log("lead_processed", id=lead["id"], tier=r["tier"], score=r["score"], scorer=r["scorer"])
    rows.sort(key=lambda x: -x["score"])
    with open(OUT / "crm_updates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print(f"{len(raw)} captured, {len(leads)} after dedupe\n")
    for x in rows:
        print(f"{x['score']:>3}  {x['tier']:<5} {x['name']:<16} {x['company'] or '-':<16} {x['reasons']}")
    from collections import Counter
    print("\n", dict(Counter(x["tier"] for x in rows)), "| scorers:", dict(Counter(x["scorer"] for x in rows)))


if __name__ == "__main__":
    main()
