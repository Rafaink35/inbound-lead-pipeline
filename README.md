# Inbound Lead Pipeline: capture, enrich, score with Claude, route

A small, production-minded demo of an AI lead-qualification workflow.
**All data is fictional.** It mirrors the pattern I build for sales teams; no real client data is used.

## What it does
1. **Capture** leads from a CSV (stand-in for a web form / webhook).
2. **Dedupe** by email hash and merge repeat requests (one lead, full context).
3. **Enrich** company data (industry, size) from a lookup (stand-in for an enrichment API).
4. **Score** each lead 0-100 with Claude against an editable ICP (`icp.json`), returning strict JSON: score, tier, reasons, CRM summary, next step.
5. **Route**: hot leads flag an owner alert, warm go to nurture, cold get no outreach.
6. **Draft** follow-up emails. **Nothing is sent automatically**: every draft needs human approval.

## Reliability choices
- Model output is validated against a schema; one retry on bad output; then a rules-based fallback with the same schema, so the pipeline never stops on an API failure.
- The scorer used (`claude` or `rules_fallback`) is stored on every row, so quality can be audited.
- Every decision is logged to `output/run_log.jsonl`.
- The model is told to say "unknown" rather than guess.

## Run
    pip install -r requirements.txt
    python pipeline.py                        # offline: rules fallback
    ANTHROPIC_API_KEY=... python pipeline.py  # scoring with Claude (CLAUDE_MODEL optional)

Outputs: `output/crm_updates.csv`, `output/followups/*.txt`, `output/run_log.jsonl`.

## Mapping to n8n / Make
Webhook (form) -> Remove Duplicates -> HTTP Request (enrichment) -> Anthropic/AI Agent node with the prompt in `build_prompt()` -> IF (tier) -> CRM node + Slack alert / Wait-for-approval -> Gmail draft.

## Honest limits
Enrichment is simulated, the CRM is a CSV, and scores from the fallback are rule-based. The Claude path follows the standard SDK call but should be run with your own key and tuned on real labeled leads before trusting thresholds.
