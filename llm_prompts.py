"""
llm_prompts.py — single auditable home for every Gemini prompt and a thin
generation wrapper.

All prompt builders return plain strings (or, where multimodal input is
needed, the caller assembles the `contents` list with the prompt text first
followed by Part objects, exactly like the StaffMeetingBuilder baseline).
"""
from google.genai import types

import config


# --------------------------------------------------------------------------- #
# Generation wrapper
# --------------------------------------------------------------------------- #
def generate(client, contents, tools=None, temperature=0.3, model=None):
    """Call Vertex AI Gemini and return the response text.

    `contents` may be a string or a list (text + Part objects). `tools` is an
    optional list of types.Tool (e.g. GoogleSearch grounding)."""
    if isinstance(contents, str):
        contents = [contents]
    request_config = types.GenerateContentConfig(temperature=temperature, tools=tools)
    response = client.models.generate_content(
        model=model or config.MODEL_NAME,
        contents=contents,
        config=request_config,
    )
    return response.text


def google_search_tool():
    """Return the GoogleSearch grounding tool list (Module A earnings pull)."""
    return [types.Tool(google_search=types.GoogleSearch())]


def _team_block(team_context):
    """Render the shared team directory (config.team_context_block()) as an
    optional prompt section. Empty string when no team context is provided."""
    if team_context and team_context.strip():
        return f"\n### TEAM CONTEXT (who's who — use for ownership, delegation, and tone)\n{team_context}\n"
    return ""


# --------------------------------------------------------------------------- #
# Module A — StaffMeetingBuilder
# --------------------------------------------------------------------------- #
def build_staff_meeting_prompt(
    prev_context_text,
    email_summary_block,
    additional_files_text,
    agenda_structure_text,
    earnings_prompt="",
    team_context="",
):
    return f"""
System: You are 'StaffMeetingBuilder', an executive assistant for {config.USER_NAME}.
{_team_block(team_context)}Reference (Last Month's Notes): {prev_context_text[:3000]}
Input Data (New Emails): {email_summary_block}
Additional File Context: {additional_files_text}

### AGENDA OUTLINE
{agenda_structure_text if agenda_structure_text else "No specific outline provided. Create logical headings."}

Instructions:
1. Extract key updates from the emails AND the Additional File Context provided. Ensure emails marked with CATEGORIES: "staff meeting include" are prioritized.
2. Do NOT include info covered in Reference notes.
3. STRUCTURE: Format your output using the exact headings provided in the AGENDA OUTLINE above. Place each extracted update under its most logically appropriate heading.
4. THE MISCELLANEOUS RULE: At the very end of the notes, you MUST create a heading titled "Miscellaneous / Other Updates". Any important action items, memos, or updates that do not clearly fit into the predefined agenda headings MUST be placed here.
5. Tone: Direct and professional. Use bullet points. Do not use overly complimentary language.
6. Remove any information that is personal to individuals including, individual compensation, individual evaluations, individual approvals or rejections.
{earnings_prompt}
"""


EARNINGS_PROMPT = (
    "7. FINANCIAL UPDATE: Use Google Search to find the most recent quarterly "
    "earnings report for Zebra Technologies (NASDAQ: ZBRA). Create a brief, "
    "3-bullet summary and map it to the appropriate agenda heading, or place it "
    "in Miscellaneous."
)


# --------------------------------------------------------------------------- #
# Module C — Executive Translator
# --------------------------------------------------------------------------- #
def build_exec_translator_prompt(
    technical_text,
    role,
    names,
    focus,
    extra_instructions="",
    email_context="",
    team_context="",
):
    """Build the audience-tailored Executive Translator prompt for one persona.

    `role`/`names`/`focus` describe the target leadership audience; `focus`
    steers what the brief emphasizes and what the follow-up questions probe.
    `extra_instructions` are ad-hoc directives from the user (e.g. "limit to 3
    sentences"); `email_context` is optional Outlook content scrubbed for the
    subject being summarized.
    """
    email_block = (
        f"\nRELATED EMAIL CONTEXT (scrubbed from {config.USER_NAME}'s Outlook on this "
        f"subject — use for additional facts; do not quote verbatim):\n{email_context}\n"
        if email_context.strip()
        else ""
    )
    extra_block = (
        f"\nADDITIONAL INSTRUCTIONS FROM {config.USER_NAME} (these take priority over "
        f"the length/format defaults below where they conflict):\n{extra_instructions}\n"
        if extra_instructions.strip()
        else ""
    )
    return f"""
System: You are the 'Executive Translator' for {config.USER_NAME}, an ME Engineering Manager.
Your job is to reduce the 4:1 translation tax between deep ME engineering work
and senior leadership by tailoring the SAME update to one specific audience.
{_team_block(team_context)}
TARGET AUDIENCE: {role}{f" ({names})" if names and names.strip() else ""}
WHAT THIS AUDIENCE CARES ABOUT: {focus}
They are smart but should not be made to wade through ME domain jargon.

RAW TECHNICAL INPUT (root cause analyses, CT scan results, L3 engineering notes):
{technical_text}
{email_block}{extra_block}
Translate the technical input FOR THIS AUDIENCE. Emphasize what matters to them
per "WHAT THIS AUDIENCE CARES ABOUT" and drop detail they don't need. Use the
executive "What, So What, Now What" framework with exactly these markdown headers:

### What (Impact)
Plain-language statement of what is happening / what was found, framed for this audience.

### So What (Risk)
Why this audience should care — the customer, financial, schedule, technical, or
reputational risk most relevant to their role.

### Now What (Timeline)
The plan, owners, and dated next steps, or the specific decision/support needed from this audience.

### Likely Follow-Up Questions
Exactly 3 questions, as a numbered list, that THIS audience is most likely to ask
in response — the things they will probe given what they care about.

Rules: Concise (leadership reads fast). Translate acronyms on first use. No raw
part numbers or lab minutiae unless they change the decision. Direct tone, no fluff.
"""


# --------------------------------------------------------------------------- #
# Phase 0 — Email Action Identifier
# --------------------------------------------------------------------------- #
def build_email_action_prompt(
    new_email_content,
    existing_context="",
    user_name=None,
    user_email=None,
    known_projects=None,
    team_context="",
):
    """Analyze a new email in the context of its thread and extract action
    items with Eisenhower prioritization, a suggested next step, a suggested
    response, and the project the email belongs to. Ported/extended from the
    EmailToJira analyzer. Returns a prompt that asks for a single JSON object:
    {extracted_tasks: [...], new_context_summary: str, Project: str}.
    """
    user_name = user_name or config.USER_NAME
    user_email = user_email or config.USER_EMAIL
    known_block = (
        "KNOWN PROJECT NAMES (reuse the EXACT name if this email fits one; only "
        "invent a new short name if none fit):\n- " + "\n- ".join(known_projects)
        if known_projects
        else "No known project names yet — assign a concise, reusable project name."
    )
    return f"""
You are an expert email action analyzer for {user_name} (email: {user_email}).
Analyze a NEW email within the context of its thread
history and identify concrete action items, who they fall on, how to prioritize
them, the single best next step, a suggested response, and which project it belongs to.
{_team_block(team_context)}
### THREAD HISTORY (PREVIOUS SUMMARY)
{existing_context or "No history available for this thread."}

### {known_block}

### NEW EMAIL CONTENT
{new_email_content[:2500]}

### YOUR TASKS
1. Analyze the 'NEW EMAIL CONTENT' only (use the history for context). Identify any action items.
2. For each action set `Requested_Of_Me` = true if {user_name} is the person being
   asked to perform it (e.g. addressed directly or in the 'To' field); else false.
3. Provide the single best `Next Step` for {user_name}.
4. Draft a `Suggested Response`:
   - If `Requested_Of_Me` is true: a concise reply {user_name} could send to acknowledge or act on the request.
   - If false: a short reply delegating/redirecting or asking the owner for status — or "No response needed" if purely informational.
5. Produce a `new_context_summary` that merges key info from the history with this email.
6. Assign a top-level `Project`: a short, stable label (e.g. "TC101", "Dalosy WT64").
   Reuse an exact KNOWN PROJECT NAME above when the email fits one. Use "Unassigned"
   for generic/admin email with no clear project.
7. Detect SHIPMENTS: if the email indicates a shipment / shipping of samples or parts
   (tracking number, carrier, "shipped", waybill, etc.), set `shipment` to an object
   with: `associated_case`, `associated_spr`, `sender` (who sent the package),
   `date_sent` (YYYY-MM-DD), `contents` (what's inside), `carrier` (FedEx | UPS | DHL |
   Other), `tracking_number`. Use "" for any field not stated. If the email is NOT
   about a shipment, set `shipment` to null.
8. Format the ENTIRE response as a single valid JSON object with exactly four keys:
   `extracted_tasks` (array), `new_context_summary` (string), `Project` (string),
   and `shipment` (object or null).

### STRICT OUTPUT SCHEMA (each item in `extracted_tasks`)
{{
  "Action": "<concise action item>",
  "Origin": "Email",
  "Email Thread": "<subject line>",
  "Due Date": "<date or 'Unspecified'>",
  "Owner": "<who owns this action>",
  "Quadrant": "Urgent | Critical | Delegate | Delete",
  "Priority": "High | Medium | Low",
  "Requested_Of_Me": true,
  "Next Step": "<the single best next step for {user_name}>",
  "Suggested Response": "<draft reply text, or 'No response needed'>"
}}

### EISENHOWER RULES (for `Quadrant`)
- 'Urgent'  : a senior leader is requesting action, OR the deadline is immediate, OR {user_name} is blocking others.
- 'Critical': core project work for {user_name} with a non-urgent deadline.
- 'Delegate': urgent BUT {user_name} is not the primary owner.
- 'Delete'  : purely informational (FYI), no action required.
The `Quadrant` value MUST be EXACTLY one of those four strings (never 'Q1' etc.).
Map `Priority` from the quadrant: Urgent->High, Critical->Medium, Delegate->Medium, Delete->Low.
If there are no action items, return an empty `extracted_tasks` array (still set `Project`).

### SHIPMENT SCHEMA (the `shipment` value — object, or null if not a shipment)
{{
  "associated_case": "", "associated_spr": "", "sender": "", "date_sent": "YYYY-MM-DD",
  "contents": "", "carrier": "FedEx | UPS | DHL | Other", "tracking_number": ""
}}
"""


def build_thread_action_prompt(thread_block, team_context=""):
    """Consolidate one email CONVERSATION (built from its per-email analyses) into a
    single summary + suggested overall next step, so the whole thread can be
    dispositioned at once. Returns a prompt asking for a single JSON object.
    """
    return f"""
You are {config.USER_NAME}'s assistant. The CONVERSATION below is one email thread,
given as the per-email analyses (date · sender · summary · tasks), oldest to newest.
Consolidate the WHOLE thread — do not repeat tasks that a later email already resolved.
{_team_block(team_context)}
### CONVERSATION (per-email analyses, oldest → newest)
{thread_block}

### YOUR TASKS
1. Write a short `summary` of where this conversation stands.
2. List `key_points` (the few facts/decisions that matter).
3. List `pending_tasks` still open across the thread (deduped; drop anything resolved later).
4. Give the single best `suggested_action` — the overall next step for the whole thread.
5. Set `requested_of_me` = true if {config.USER_NAME} is the one being asked to act.
6. Recommend a `suggested_disposition` for the thread, one of:
   "tracked" (worth adding to the action tracker), "read_no_action", "delegated",
   "follow_up", "ignore", or "" if unsure.

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "summary": "",
  "key_points": ["..."],
  "pending_tasks": ["..."],
  "suggested_action": "",
  "suggested_disposition": "tracked | read_no_action | delegated | follow_up | ignore | ",
  "requested_of_me": true
}}
Be concise and direct. Do not invent items not present in the conversation.
"""


def build_shipping_status_prompt(carrier, tracking_number, raw_text):
    """Interpret raw carrier-tracking page text (or API response) into a clean,
    one-line shipment status. Returns a prompt asking for {"status": "..."}."""
    return f"""
You are reading the public tracking page for a {carrier or "courier"} shipment
(tracking number {tracking_number}). From the RAW CONTENT below, extract the
current shipment status as ONE concise line (e.g. "In transit — Memphis, TN
(est. delivery Jun 24)" or "Delivered Jun 20"). If the content has no usable
tracking status (blank, blocked, or a JavaScript shell), return exactly
"Unknown — see tracking link".

RAW CONTENT:
{(raw_text or "")[:6000]}

Return a SINGLE valid JSON object: {{"status": "<one-line status>"}}.
"""


def build_project_themes_prompt(project, emails_block, prior_themes=""):
    """Synthesize the common themes across one project's emails: the type of
    work being requested and whether work is repetitive vs. prior emails/themes.
    `emails_block` is per-email summaries (subject · date · summary), NOT full
    bodies. `prior_themes` is the last synthesis JSON, used to judge repetition.
    Asks for a single JSON object: {project_summary: str, themes: [...]}.
    """
    prior = (
        "\nPRIOR THEMES (from an earlier synthesis run — judge repetition against these):\n"
        f"{prior_themes}\n"
        if prior_themes and prior_themes.strip()
        else ""
    )
    return f"""
System: You are synthesizing the email activity for the project "{project}" for
{config.USER_NAME}, an ME Engineering Manager. Surface the common themes and the
TYPE of work being requested, and flag whether work is repetitive versus prior
emails/themes in this project.

EMAILS IN THIS PROJECT (subject · date · summary):
{emails_block}
{prior}
Return a SINGLE valid JSON object with exactly these keys:
{{
  "project_summary": "<2-3 sentence overview of what this project is about and its current state>",
  "themes": [
    {{
      "theme": "<short theme label>",
      "type_of_work": "<kind of work being requested, e.g. root-cause analysis, design review, status reporting, data request>",
      "example_emails": ["<short subject ref>", "..."],
      "repetition_note": "<is this recurring vs prior emails/themes? note what is repetitive and what is new>"
    }}
  ]
}}

Rules: Be concise and direct. Do not invent emails not listed above. If activity
is light, return a single theme. Base `repetition_note` on the PRIOR THEMES and the
recurrence visible across the listed emails.
"""


# --------------------------------------------------------------------------- #
# 1:1 Meeting Prep Assistant
# --------------------------------------------------------------------------- #
def build_one_on_one_by_project_prompt(
    member_name, member_function, scope_label, items_block, team_context="",
):
    """Synthesize a 1:1 briefing for a direct report, ORGANIZED BY PROJECT.

    `items_block` is the report's cached items already grouped under project
    headers (SPR#/Jira key/project name), each line tagged with its source
    (jira/email/action). Items under "Unassigned" have no structured project
    tag — cluster them into the most fitting project above, or an "Other" group.
    PTO is handled separately by the page and is NOT included here.

    Returns a prompt asking for a single JSON object with an overview plus a
    per-project array. Neutral, fact-based; discussion topics only (HITL)."""
    return f"""
System: You are preparing {config.USER_NAME}'s 1:1 with {member_name}
({member_function or "team member"}), covering {scope_label}. Synthesize the data
below into a concise, neutral, fact-based briefing ORGANIZED BY PROJECT — discussion
topics only, no fluff, no invented facts.
{_team_block(team_context)}
### GATHERED ITEMS (grouped by project; each line tagged with its source)
{items_block or "None."}

Group your output by project. Keep the project names given above (a project is
typically an SPR#/Jira key like ECRT-1234, or a named initiative). For items listed
under "Unassigned", assign each to the most fitting project above based on its
content, or collect them into a single project named "Other" if none fits.

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "overview": "<2-3 sentence overall summary of this report's period across all projects>",
  "projects": [
    {{
      "project": "<SPR# / Jira key / project name>",
      "summary": "<one-line current status of this project>",
      "accomplishments": "<md bullets: completed tickets + milestones reached>",
      "next_steps": "<md bullets: active priorities, each with its next step>",
      "roadblocks": "<md bullets: schedule slips, blocked/overdue items, help requests>",
      "discussion_topics": "<md bullets: notable email threads / talking points; EXCLUDE admin/meeting-invite noise>"
    }}
  ]
}}
Rules: "projects" MUST be a JSON array, ordered most-active first. Use markdown
bullet lists inside each string; if a sub-section has nothing, say so briefly. Do
not invent tickets, emails, or projects not present in the data above.
"""


def build_goal_appraisal_prompt(doc_text):
    """Parse a team member's goal-appraisal document (text extracted from a PDF /
    Excel / doc) into its LEAF sub-goals. A leaf is a numbered goal that carries an
    actual objective and (usually) an assessment — NOT a section heading, a roll-up
    row, or a row marked 'This Field Intentionally Left Blank'. Returns a prompt
    asking for a single JSON array, one object per leaf goal."""
    return f"""
System: You are extracting the individual goals from a performance goal-appraisal
document for {config.USER_NAME}'s team member. The document is organized into
sections (Growth, Execution, Culture) and numbered goals with sub-goals.

### APPRAISAL DOCUMENT (extracted text)
{doc_text}

Extract ONLY the LEAF goals — the most granular numbered rows that state an actual
objective. EXCLUDE:
- section headings (e.g. "1.0 Growth", "5.1 ECRT Core KPIs"),
- roll-up rows and any row whose remark is "This Field Intentionally Left Blank",
- rows with no real goal text.

Return a SINGLE valid JSON array. Each element:
{{
  "index": "<the leaf number exactly as shown, e.g. 1.3.1.2.1 or 5.1.4>",
  "category": "<the top-level section this rolls up to: Growth | Execution | Culture>",
  "goal": "<the goal's objective text, trimmed to one or two sentences>",
  "remarks": "<the member's remark/status note for this goal, or '' if none>",
  "assessment": "<the appraisal % if present, e.g. '50%', else ''>"
}}

Rules: Output ONLY the JSON array (no prose). Preserve the leaf `index` verbatim so
it stays a stable key across re-imports. Do not invent goals not present in the text.
If a value is missing, use an empty string.
"""


def build_manager_prep_prompt(
    issues_block, special_projects_block, pto_block, calendar_block,
    personal_block, period, team_context="",
):
    """Synthesize an UPWARD 1:1 briefing: the user preparing for their 1:1 with
    their own manager. Unlike the direct-report prep, this arms the user with
    data-backed answers and predicts what the manager will likely ask about the
    Critical/Aged issues. Returns a prompt asking for a single JSON object.
    Neutral, fact-based; no invented facts (only use the data provided)."""
    user, manager = config.USER_NAME, config.MANAGER_NAME
    # Title/org is profile-configurable and often unset — render the
    # parenthetical only when we actually have one.
    title = (config.MANAGER_TITLE or "").strip()
    manager_ref = f"{manager} ({title})" if title else manager
    return f"""
System: You are preparing {user}'s 1:1 with their manager,
{manager_ref}, covering {period}. Your job is
to ARM {user} with concise, data-backed talking points and to PREDICT
what {manager} will most likely ask, so {user} walks in
prepared. Neutral, fact-based, no fluff, no invented facts — use only the data
below. If a fact is unknown, say so rather than guessing.
{_team_block(team_context)}
### CRITICAL & AGED ISSUES (from the manager's dashboard, enriched with Jira analysis)
{issues_block or "None provided."}

### SPECIAL PROJECTS — recent email context (each line tagged [Project])
{special_projects_block or "None."}

### UPCOMING TEAM TIME OFF (next 2 weeks, from the calendar)
{pto_block or "None."}

### CALENDAR LOGISTICS (next 2 weeks)
{calendar_block or "None."}

### {user}'S RECENT INBOX (scan for implicit personal concerns —
### workload/capacity limits, HR topics, career/training, recurring friction)
{personal_block or "None."}

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "critical_issues": [
    {{
      "name": "<issue/ticket key + short title>",
      "status": "<current status / engineering phase>",
      "blocker": "<current blocker, or 'None' >",
      "updates": "<most recent meaningful update from Jira/email>",
      "predicted_question": "<the specific question {manager} is most likely to ask about this issue>",
      "response": "<{user}'s pre-armed, data-backed answer/talking point>"
    }}
  ],
  "special_projects": "<markdown: one bold-labelled bullet per project found in the email context above (group by the [Project] tag), each a one-line status. Omit projects with no email context.>",
  "team_ops": "<markdown: PTO/FTO bullets, then key calendar logistics/overlaps for the next 2 weeks>",
  "personal_concerns": "<markdown bullets: suggested topics auto-extracted from the inbox sift (workload, HR, career/training, recurring bottlenecks). Frame as discussion topics for {user} to raise.>"
}}
Rules: "critical_issues" MUST be a JSON array (one object per issue from the data
above; empty array if none). The other three values are markdown strings using
bullet lists. If a section has nothing, say so briefly. Do not invent issues,
projects, or concerns that are not supported by the data.
"""


# --------------------------------------------------------------------------- #
# Module F — Admin & OOO Hub
# --------------------------------------------------------------------------- #
def build_availability_digest_prompt(notices_text):
    return f"""
System: Consolidate the scattered team availability notices below (OOO / VTO /
PTO / partial-day) into a single clean daily digest for {config.USER_NAME}.

RAW NOTICES (from calendar events and/or Outlook emails):
{notices_text}

Output a markdown table with columns: Person | Dates | Type | Notes.
- One row per person per absence. Merge duplicates.
- Type is one of OOO, VTO, PTO, Partial Day.
- Sort by start date. If a date is unclear, write "TBD" — do not guess.
Below the table, add a one-line "Coverage watch:" note flagging any day where
multiple people are out, if applicable.
"""


# --------------------------------------------------------------------------- #
# Phase 3 — Jira State Tracking & Analysis
# --------------------------------------------------------------------------- #
def build_jira_state_prompt(ticket_block, attachments_text="", phases=None, team_context=""):
    """Extract the true engineering state of an ECRT Jira ticket from its
    description, timestamped comment history, and (optionally) attachment text.
    `phases` is the canonical lifecycle list (config.JIRA_PHASES). Returns a
    prompt asking for a single JSON object describing phase, root-cause/re-design
    status, milestones, schedule slips, action audits, and a digest.
    """
    phases = phases or config.JIRA_PHASES
    phase_list = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(phases))
    attach_block = (
        f"\n### ATTACHMENT CONTENT (meeting minutes, test/FA reports, etc.)\n{attachments_text}\n"
        if attachments_text and attachments_text.strip()
        else ""
    )
    return f"""
System: You are the 'ECRT Jira State Tracking Agent' for the Mechanical Engineering
team. Standard Jira status fields do NOT capture the true state of hardware
investigations, so determine the real state from the description, the timestamped
comment history, and any attachment content below.

### ECRT ENGINEERING LIFECYCLE PHASES
{phase_list}
{_team_block(team_context)}

### TICKET (description + comments as `[created] author: body`)
{ticket_block}
{attach_block}
### YOUR TASKS
1. Determine the ticket's CURRENT phase (one of the phase names above), supplementing/overriding the Jira status. Justify briefly.
2. State whether the Root Cause is known, unknown, or partial — with the supporting detail.
3. Determine whether Re-Design is complete, ongoing, pending, or n/a.
4. Extract forward-looking target dates: manufacturing change, new/replacement parts to the customer, and overall completion.
5. Build a milestone list with dates. Classify each as: "past" (already happened), "future" (scheduled ahead), or "slipped" (a target that was missed or pushed). Cite the comment/attachment excerpt where each date was established.
6. Detect schedule slips: compare dates restated in NEWER comments against dates committed in OLDER comments (use the timestamps). Report original date, new date, and the slip in days.
7. Aggregate the currently OPEN "Next Steps" stated in recent comments.
8. Audit commitments: for action items made in OLDER comments, scan subsequent updates to judge whether each was concluded ("concluded"), still "open", or "unknown".
9. From attachment content (if any), list identified actions/recommendations and cross-reference the ticket history to judge each conclusion ("resolved"/"open"/"unknown").
10. Write a concise markdown `digest` covering true state, slip metrics, unresolved items, and current target dates.
11. Build a `fishbone` root-cause analysis: a concise `problem` statement (the SPR's core problem / fish head) and, for EACH of the six manufacturing categories — Manpower, Machine, Material, Method, Measurement, Maintenance — the contributing causes evidenced in the ticket/attachments. Dig for true root causes (ask "why?"), not just symptoms. If a category shows NO evidence of investigation, return an EMPTY array for it (the UI marks it "Not Investigated"). Keep each cause short.
12. Derive the root cause with the 5 WHYS technique, drawing on ALL ticket content — the description, comments, attachments, AND every field under "OTHER JIRA FIELDS" (especially any "Root Cause Description" field). State the `problem`, then the ordered chain of `whys` (each entry answers "why did the previous happen?", up to 5), and the final `root_cause`. Set `status`: "complete" only if the chain reaches a true, evidenced root cause; "incomplete" if the chain is partial / stops at a symptom; "insufficient" if there is not enough information to begin. When not complete, still include the whys you could derive and leave `root_cause` empty.

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "current_phase": "<one of the phase names above>",
  "phase_rationale": "<1-2 sentences>",
  "root_cause_status": "known | unknown | partial",
  "root_cause_detail": "<short>",
  "root_cause_5whys": {{"problem": "", "whys": ["why 1 answer", "why 2 answer"], "root_cause": "", "status": "complete | incomplete | insufficient"}},
  "redesign_status": "complete | ongoing | pending | n/a",
  "target_dates": {{"manufacturing_change": "YYYY-MM-DD or ''", "customer_delivery": "YYYY-MM-DD or ''", "overall_completion": "YYYY-MM-DD or ''"}},
  "milestones": [ {{"label": "", "date": "YYYY-MM-DD", "type": "past | future | slipped", "source": "<excerpt where the date was set>"}} ],
  "schedule_slips": [ {{"milestone": "", "original_date": "YYYY-MM-DD", "new_date": "YYYY-MM-DD", "slip_days": 0, "evidence": ""}} ],
  "open_next_steps": ["..."],
  "comment_action_audit": [ {{"action": "", "committed": "<date or comment ref>", "status": "concluded | open | unknown", "evidence": ""}} ],
  "attachment_actions": [ {{"source_file": "", "action": "", "conclusion": "resolved | open | unknown"}} ],
  "fishbone": {{"problem": "", "categories": {{"Manpower": [], "Machine": [], "Material": [], "Method": [], "Measurement": [], "Maintenance": []}}}},
  "digest": "<concise markdown>"
}}

Rules: dates as YYYY-MM-DD, or "" when unknown — do NOT guess. Base slips on the
comment timestamps. Do not invent facts not present in the inputs. Use empty arrays
where a section has no items.
"""


def build_jira_state_delta_prompt(
    prior_analysis_json, base_block, new_block, phases=None, team_context=""
):
    """Incrementally UPDATE a prior Jira state analysis given only the new
    content since it was produced. `prior_analysis_json` is the previous analysis
    object (pretty-printed JSON string); `base_block` is a compact reminder of the
    ticket identity (summary, status, and the description only if it changed);
    `new_block` is the delta — new comments (with timestamps), new/changed custom
    fields, and any attachment content. Returns the SAME JSON schema as
    build_jira_state_prompt so the UI and parser are unaffected."""
    phases = phases or config.JIRA_PHASES
    phase_list = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(phases))
    return f"""
System: You are the 'ECRT Jira State Tracking Agent' for the Mechanical Engineering
team. You previously analyzed this ticket and produced the PRIOR ANALYSIS below.
New activity has since occurred. UPDATE the prior analysis to reflect ONLY the new
content — do NOT rebuild from scratch and do NOT drop conclusions that the new
content does not change. Carry forward everything still valid.

### ECRT ENGINEERING LIFECYCLE PHASES
{phase_list}
{_team_block(team_context)}

### PRIOR ANALYSIS (your previous output — the baseline to update)
{prior_analysis_json}

### TICKET IDENTITY (description shown only if it changed)
{base_block}

### NEW SINCE LAST ANALYSIS (the delta — analyze these against the prior analysis)
{new_block}

### YOUR TASKS
Re-evaluate the prior analysis in light of the NEW content and produce a fully
updated analysis:
1. Re-assess the CURRENT phase — advance it only if the new content shows the work moved on; otherwise keep it.
2. Update Root Cause status/detail, the 5-Whys chain, and Re-Design status if the new content adds evidence; otherwise carry them forward.
3. Update target dates (manufacturing change, customer delivery, overall completion) if newer dates were stated.
4. Add NEW milestones from the new content and re-classify existing ones (past/future/slipped) using the latest dates. Keep prior milestones unless the new content supersedes them.
5. Detect NEW schedule slips by comparing dates in the new comments against dates committed earlier (in the prior analysis or older comments). Keep previously reported slips.
6. Refresh `open_next_steps` to the steps currently open per the newest comments.
7. Audit commitments: for actions previously "open", check the new content to see if any are now "concluded"; add any new commitments.
8. Fold in any attachment actions/recommendations from the new content.
9. Extend the `fishbone` with newly evidenced causes (keep prior causes; only add).
10. Update the markdown `digest` to reflect the current true state, including any new slips/unresolved items.

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "current_phase": "<one of the phase names above>",
  "phase_rationale": "<1-2 sentences>",
  "root_cause_status": "known | unknown | partial",
  "root_cause_detail": "<short>",
  "root_cause_5whys": {{"problem": "", "whys": ["why 1 answer", "why 2 answer"], "root_cause": "", "status": "complete | incomplete | insufficient"}},
  "redesign_status": "complete | ongoing | pending | n/a",
  "target_dates": {{"manufacturing_change": "YYYY-MM-DD or ''", "customer_delivery": "YYYY-MM-DD or ''", "overall_completion": "YYYY-MM-DD or ''"}},
  "milestones": [ {{"label": "", "date": "YYYY-MM-DD", "type": "past | future | slipped", "source": "<excerpt where the date was set>"}} ],
  "schedule_slips": [ {{"milestone": "", "original_date": "YYYY-MM-DD", "new_date": "YYYY-MM-DD", "slip_days": 0, "evidence": ""}} ],
  "open_next_steps": ["..."],
  "comment_action_audit": [ {{"action": "", "committed": "<date or comment ref>", "status": "concluded | open | unknown", "evidence": ""}} ],
  "attachment_actions": [ {{"source_file": "", "action": "", "conclusion": "resolved | open | unknown"}} ],
  "fishbone": {{"problem": "", "categories": {{"Manpower": [], "Machine": [], "Material": [], "Method": [], "Measurement": [], "Maintenance": []}}}},
  "digest": "<concise markdown>"
}}

Rules: dates as YYYY-MM-DD, or "" when unknown — do NOT guess. Do not invent facts
not present in the prior analysis or the new content. Use empty arrays where a
section has no items.
"""


# --------------------------------------------------------------------------- #
# OoO Management — normalize system emails + calendar invites
# --------------------------------------------------------------------------- #
def build_ooo_parse_prompt(emails_block, calendar_block, roster, team_directory=""):
    """Normalize HR system OoO approval emails and calendar OoO invites into
    structured per-request records for reconciliation. Each input item is
    prefixed with an `id`; the response must echo that id so the page can tie
    rows back to their source. `team_directory` (name — function (email, ID))
    helps map calendar organizers/email senders to the canonical roster name.
    Returns a prompt asking for a single JSON object.
    """
    roster_list = ", ".join(roster) if roster else "(none configured)"
    directory_block = (
        f"\nMEMBER DIRECTORY (use names AND emails to resolve who a record is "
        f"about, then output the matching roster name):\n{team_directory}\n"
        if team_directory and team_directory.strip()
        else ""
    )
    return f"""
System: You are normalizing Out-of-Office (OoO) records for an ME manager so two
sources can be reconciled. Extract structured records — do not editorialize.

TEAM ROSTER (map every record to ONE of these exact names, or "Other"):
{roster_list}
{directory_block}

### SYSTEM APPROVAL EMAILS (id | received | sender | subject | body)
{emails_block or "(none)"}

### CALENDAR OoO INVITES (id | organizer | subject | start | end)
{calendar_block or "(none)"}

### TASKS
- For each SYSTEM email, output a `system_requests` record: the employee it is
  about (mapped to a roster name), the leave type, the date range it covers, and
  a short detail. An automated system often sends MULTIPLE single-day emails for
  one absence — keep them as separate records (reconciliation handles merging).
- For each CALENDAR invite, output a `calendar_invites` record: the member (from
  the organizer, mapped to a roster name), type, date range, and a short detail.
  A single invite may span MULTIPLE days — capture the true start/end.
- Map the leave `type` to one of: PTO, FTO, VTO, OOO (best fit).

Return a SINGLE valid JSON object with EXACTLY these keys:
{{
  "system_requests": [ {{"id": "", "member": "", "type": "PTO|FTO|VTO|OOO", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "details": ""}} ],
  "calendar_invites": [ {{"id": "", "member": "", "type": "PTO|FTO|VTO|OOO", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "details": ""}} ]
}}

Rules: dates as YYYY-MM-DD; single-day → start_date == end_date; if a date is
unclear leave it "" — do NOT guess. Echo each input `id` exactly. Use empty
arrays when a source has no items.
"""
