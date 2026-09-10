"""
ServiceNow connector, exposed as a small set of MCP-style "tools".

Each public function here is written to be directly callable as an MCP tool
(name, docstring, typed args/return) so this module can be dropped behind an
actual MCP server (e.g. a `servicenow-mcp` stdio/SSE server) with no change to
the callers in this app. Today it talks to the ServiceNow Table API directly
over basic auth, which is the same data an MCP server would ultimately proxy.
"""
import logging
from typing import Optional

import requests

import storage
from config import Config

logger = logging.getLogger(__name__)


def get_effective_instance_url() -> str:
    """DB override (Settings -> ServiceNow Connection) takes priority over
    .env, and takes effect on the very next call — no restart needed."""
    return (storage.get_servicenow_config().get("instance_url") or Config.SERVICENOW_INSTANCE_URL).rstrip("/")


def get_effective_username() -> str:
    return storage.get_servicenow_config().get("username") or Config.SERVICENOW_USERNAME


def get_effective_password() -> str:
    return storage.get_servicenow_config().get("password") or Config.SERVICENOW_PASSWORD


# Short internal aliases used throughout this module
_instance_url = get_effective_instance_url
_username = get_effective_username
_password = get_effective_password

INCIDENT_FIELDS = [
    "number",
    "sys_id",
    "short_description",
    "description",
    "close_notes",
    "resolution_notes",
    "state",
    "priority",
    "category",
    "subcategory",
    "assignment_group",
    "cmdb_ci",
    "opened_at",
    "closed_at",
    "sys_updated_on",
    "caller_id",
    "opened_by",
    "assigned_to",
]


def build_incident_url(sys_id: str) -> str:
    """Direct link to an incident's ServiceNow form (same `<table>.do?sys_id=`
    pattern tool_create_problem already uses for Problem tickets)."""
    if not sys_id:
        return ""
    return f"{_instance_url()}/incident.do?sys_id={sys_id}"


def build_service_request_url(sys_id: str) -> str:
    if not sys_id:
        return ""
    return f"{_instance_url()}/sc_req_item.do?sys_id={sys_id}"


def build_kb_url(sys_id: str) -> str:
    if not sys_id:
        return ""
    return f"{_instance_url()}/kb_knowledge.do?sys_id={sys_id}"


def _normalize_incident(row: dict) -> dict:
    """ServiceNow's incident table names its CI reference field `cmdb_ci`;
    every caller in this app (Triage, Reporting, ICA Client) expects
    `configuration_item`, matching the naming already normalized for service
    requests (see _normalize_request). Also attaches a direct ServiceNow
    form link (`url`) so every incident flowing through this app carries a
    ready-made link — pipelines that trim the incident dict for their state
    just need to copy this one field through."""
    row = dict(row)
    row["configuration_item"] = row.pop("cmdb_ci", "")
    row["url"] = build_incident_url(row.get("sys_id"))
    return row


def _session() -> requests.Session:
    s = requests.Session()
    s.auth = (_username(), _password())
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s


def _flatten_reference_fields(row: dict) -> dict:
    """With sysparm_display_value=true, ServiceNow still returns reference
    fields (assignment_group, configuration_item, sys_created_by, ...) as
    {"display_value": <name>, "link": <url>} objects rather than a plain
    string. Every caller in this app treats incident/CI/group fields as
    plain strings, so flatten every such object to just its display value
    at the source — one place instead of every consumer."""
    flat = {}
    for key, value in row.items():
        if isinstance(value, dict) and "display_value" in value:
            flat[key] = value.get("display_value") or ""
        else:
            flat[key] = value
    return flat


def _get_results(url: str, params: dict, timeout: int = 30) -> list:
    """GET a Table API list endpoint. Always requests display values for
    reference fields (assignment_group, configuration_item, etc.) — without
    this, ServiceNow returns {"link": ..., "value": <sys_id>} for every
    reference field instead of its human-readable name, which would leak raw
    sys_ids into prompts and UI instead of names like "Network Support"."""
    merged = {**params, "sysparm_display_value": "true"}
    resp = _session().get(url, params=merged, timeout=timeout)
    resp.raise_for_status()
    return [_flatten_reference_fields(row) for row in resp.json().get("result", [])]


def tool_get_incident(number: str) -> Optional[dict]:
    """MCP tool: fetch a single incident record by its INC number."""
    url = f"{_instance_url()}/api/now/table/incident"
    params = {
        "sysparm_query": f"number={number}",
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": "1",
    }
    results = _get_results(url, params)
    return _normalize_incident(results[0]) if results else None


def tool_list_open_incidents(limit: int = 200) -> list:
    """MCP tool: list active (not resolved/closed) incidents, most recently updated first."""
    url = f"{_instance_url()}/api/now/table/incident"
    params = {
        "sysparm_query": "active=true^ORDERBYDESCsys_updated_on",
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": str(limit),
    }
    return [_normalize_incident(r) for r in _get_results(url, params)]


def tool_list_recent_incidents(days: int = 30, limit: int = 150) -> list:
    """MCP tool: list incidents (any state) created in the last N days, most
    recent first. Feeds the Incident Correlation solution's ServiceNow data
    source — correlation looks for recurring patterns across a time window,
    not just currently-open tickets."""
    url = f"{_instance_url()}/api/now/table/incident"
    params = {
        "sysparm_query": f"sys_created_on>=javascript:gs.daysAgoStart({int(days)})^ORDERBYDESCsys_created_on",
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": str(limit),
    }
    return [_normalize_incident(r) for r in _get_results(url, params, timeout=60)]


def tool_list_closed_incidents(limit: int = 500, offset: int = 0) -> list:
    """MCP tool: page through Closed/Resolved incidents that carry resolution notes."""
    url = f"{_instance_url()}/api/now/table/incident"
    params = {
        # state 6 = Resolved, 7 = Closed in the default ServiceNow install
        "sysparm_query": "stateIN6,7^close_notesISNOTEMPTY^ORresolution_notesISNOTEMPTY",
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": str(limit),
        "sysparm_offset": str(offset),
    }
    return [_normalize_incident(r) for r in _get_results(url, params, timeout=60)]


def tool_list_incidents_by_numbers(numbers: list) -> list:
    """MCP tool: fetch a specific, caller-provided set of incidents by INC
    number in one round trip — e.g. so Audit Compliance can be scoped to a
    handful of tickets instead of scanning the whole table."""
    numbers = [n.strip() for n in (numbers or []) if n and n.strip()]
    if not numbers:
        return []
    url = f"{_instance_url()}/api/now/table/incident"
    params = {
        "sysparm_query": f"numberIN{','.join(numbers)}",
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": str(len(numbers)),
    }
    return [_normalize_incident(r) for r in _get_results(url, params)]


def tool_list_incidents_in_range(start_date: str, end_date: str, limit: int = 500) -> list:
    """MCP tool: list incidents (any state) opened within an explicit
    [start_date, end_date] window (YYYY-MM-DD, inclusive), most recent
    first. Unlike tool_list_recent_incidents (a relative "last N days"
    window), this lets a caller scope work to a specific incident timeline —
    e.g. so Audit Compliance can be run over one week or one incident
    instead of the entire open+closed volume."""
    url = f"{_instance_url()}/api/now/table/incident"
    query = (
        f"opened_at>={start_date} 00:00:00"
        f"^opened_at<={end_date} 23:59:59"
        "^ORDERBYDESCopened_at"
    )
    params = {
        "sysparm_query": query,
        "sysparm_fields": ",".join(INCIDENT_FIELDS),
        "sysparm_limit": str(limit),
    }
    return [_normalize_incident(r) for r in _get_results(url, params, timeout=60)]


def tool_add_work_note(number: str, note_text: str) -> bool:
    """MCP tool: append a Work Note to an incident (human-reviewed AI solution
    recommendation). Uses the incident's sys_id (looked up by number) rather
    than trusting a client-supplied one. Does not change incident state —
    purely additive/non-destructive."""
    incident = tool_get_incident(number)
    if not incident or not incident.get("sys_id"):
        raise ValueError(f"Incident '{number}' was not found in ServiceNow.")

    url = f"{_instance_url()}/api/now/table/incident/{incident['sys_id']}"
    resp = _session().patch(url, json={"work_notes": note_text}, timeout=30)
    resp.raise_for_status()
    return True


def tool_create_problem(short_description: str, description: str, category: str = "",
                         related_incident_numbers: Optional[list] = None) -> dict:
    """MCP tool: create a Problem record (human-reviewed AI-drafted description)
    and best-effort link each related incident's `problem_id` field to it.
    Returns {number, sys_id, url, linked_incidents, link_failures}."""
    url = f"{_instance_url()}/api/now/table/problem"
    body = {"short_description": short_description, "description": description}
    if category:
        body["category"] = category

    resp = _session().post(url, json=body, timeout=30)
    resp.raise_for_status()
    problem = resp.json().get("result", {})
    problem_sys_id = problem.get("sys_id")
    problem_number = problem.get("number")

    linked, failed = [], []
    for inc_number in (related_incident_numbers or []):
        try:
            incident = tool_get_incident(inc_number)
            if not incident or not incident.get("sys_id"):
                failed.append(inc_number)
                continue
            patch_url = f"{_instance_url()}/api/now/table/incident/{incident['sys_id']}"
            patch_resp = _session().patch(patch_url, json={"problem_id": problem_sys_id}, timeout=30)
            patch_resp.raise_for_status()
            linked.append(inc_number)
        except Exception:
            logger.exception("Failed to link incident %s to new problem %s", inc_number, problem_number)
            failed.append(inc_number)

    return {
        "number": problem_number,
        "sys_id": problem_sys_id,
        "url": f"{_instance_url()}/problem.do?sys_id={problem_sys_id}",
        "linked_incidents": linked,
        "link_failures": failed,
    }


def tool_test_connection() -> bool:
    """MCP tool: verify credentials/URL by requesting a single incident row."""
    url = f"{_instance_url()}/api/now/table/incident"
    resp = _session().get(url, params={"sysparm_limit": "1"}, timeout=15)
    return resp.status_code == 200


# ServiceNow incident state values default to numeric codes: 6 = Resolved,
# 7 = Closed, 8 = Cancelled. Triage/Sentiment must not act on/re-analyze an
# incident that's already past this point.
CLOSED_OR_RESOLVED_STATE_CODES = {"6", "7", "8"}


def is_closed_or_resolved(incident: dict) -> bool:
    """True if an incident's state means Triage should refuse to act on it
    (Closed, Resolved, or Cancelled)."""
    return str((incident or {}).get("state", "")).strip() in CLOSED_OR_RESOLVED_STATE_CODES


def tool_update_incident(number: str, fields: dict) -> bool:
    """MCP tool: PATCH arbitrary fields (e.g. configuration_item,
    assignment_group) onto an incident by number. Used by Triage's
    human-reviewed apply step and Chatbot's apply_triage_assignment tool."""
    incident = tool_get_incident(number)
    if not incident or not incident.get("sys_id"):
        raise ValueError(f"Incident '{number}' was not found in ServiceNow.")

    url = f"{_instance_url()}/api/now/table/incident/{incident['sys_id']}"
    resp = _session().patch(url, json=fields, timeout=30)
    resp.raise_for_status()
    return True


def tool_get_incident_journal_entries(number: str) -> list:
    """MCP tool: return an incident's comment/work_notes journal entries
    (sys_journal_field), oldest first, each as
    {value, sys_created_by, sys_created_on, element}. Falls back to an empty
    list if the incident has no sys_id (e.g. not found) or the journal table
    is unavailable — callers fall back to the incident's flat
    work_notes/comments/close_notes fields in that case."""
    incident = tool_get_incident(number)
    if not incident or not incident.get("sys_id"):
        return []

    url = f"{_instance_url()}/api/now/table/sys_journal_field"
    params = {
        "sysparm_query": f"element_id={incident['sys_id']}^ORDERBYsys_created_on",
        "sysparm_fields": "value,sys_created_by,sys_created_on,element",
        "sysparm_limit": "500",
    }
    return _get_results(url, params)


def tool_list_configuration_items(limit: int = 500) -> list:
    """MCP tool: list Configuration Items (cmdb_ci) — name/short_description/
    category — used to enrich the Triage prompt's CI directory."""
    url = f"{_instance_url()}/api/now/table/cmdb_ci"
    params = {
        "sysparm_fields": "sys_id,name,short_description,category",
        "sysparm_limit": str(limit),
    }
    return _get_results(url, params)


def tool_get_configuration_item(name: str) -> Optional[dict]:
    """MCP tool: fetch a single Configuration Item by name."""
    url = f"{_instance_url()}/api/now/table/cmdb_ci"
    params = {
        "sysparm_query": f"name={name}",
        "sysparm_fields": "sys_id,name,short_description,category",
        "sysparm_limit": "1",
    }
    results = _get_results(url, params)
    return results[0] if results else None


def tool_create_incident(short_description: str, description: str) -> dict:
    """MCP tool: create a new incident. Returns {number, sys_id}. Used by the
    Chatbot's create_incident tool, gated behind its own governance rule
    (check_prior_solutions must run first in the same conversation turn)."""
    url = f"{_instance_url()}/api/now/table/incident"
    body = {"short_description": short_description, "description": description}
    resp = _session().post(url, json=body, timeout=30)
    resp.raise_for_status()
    created = resp.json().get("result", {})
    return {"number": created.get("number"), "sys_id": created.get("sys_id")}


def tool_search_kb(query: str, limit: int = 5) -> list:
    """MCP tool: keyword search over published KB articles (kb_knowledge),
    matching `query` against short_description. Best-effort — the Chatbot's
    search_knowledge_base tool falls back to saying nothing was found rather
    than failing the conversation turn if this table isn't reachable."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge"
    params = {
        "sysparm_query": f"short_descriptionLIKE{query}^workflow_state=published",
        "sysparm_fields": "number,short_description,text,sys_id",
        "sysparm_limit": str(limit),
    }
    return _get_results(url, params)


def tool_get_kb_by_number(number: str) -> Optional[dict]:
    """MCP tool: exact lookup of a single KB article by its human-readable
    number (e.g. "KB0010020"). tool_search_kb only ever LIKE-matches against
    short_description, so a query that IS the number itself (not words that
    happen to appear in the description) never matched anything — this is
    the direct, exact path. No workflow_state filter, so a draft article is
    still findable by its own number."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge"
    params = {
        "sysparm_query": f"number={number}",
        "sysparm_fields": "number,short_description,text,category,sys_id",
        "sysparm_limit": "1",
    }
    results = _get_results(url, params)
    return results[0] if results else None


def tool_list_kb_articles(limit: int = 500, offset: int = 0) -> list:
    """MCP tool: page through published Knowledge Base articles (kb_knowledge)
    with their full body text — feeds kb_ingest.py's KB vector index, which
    Incident Resolution's Agent 2 searches alongside the closed-incident
    index (see kb_ingest.py / agents/retrieval_agent.py)."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge"
    params = {
        "sysparm_query": "workflow_state=published",
        "sysparm_fields": "number,short_description,text,category,sys_id,sys_updated_on",
        "sysparm_limit": str(limit),
        "sysparm_offset": str(offset),
    }
    return _get_results(url, params, timeout=60)


def tool_get_kb_article(sys_id: str) -> Optional[dict]:
    """MCP tool: fetch a single KB article's current content by sys_id,
    regardless of workflow_state (draft or published) — used by KB
    Management's HITL review to show the reviewer what an "update" action
    would actually be overwriting."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge/{sys_id}"
    params = {"sysparm_fields": "number,short_description,text,category,workflow_state,sys_id"}
    resp = _session().get(url, params={**params, "sysparm_display_value": "true"}, timeout=30)
    resp.raise_for_status()
    result = resp.json().get("result")
    return _flatten_reference_fields(result) if result else None


def tool_create_kb_draft(short_description: str, html_body: str, category: str = "") -> dict:
    """MCP tool: create a draft KB article (kb_knowledge). Left in
    ServiceNow's default draft workflow_state — never auto-published, matching
    KB Management's HITL gate (a human always reviews before publish)."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge"
    body = {"short_description": short_description[:150], "text": html_body}
    if category:
        body["category"] = category
    resp = _session().post(url, json=body, timeout=30)
    resp.raise_for_status()
    created = resp.json().get("result", {})
    return {"number": created.get("number"), "sys_id": created.get("sys_id")}


def tool_update_kb(sys_id: str, fields: dict) -> bool:
    """MCP tool: PATCH fields (e.g. text, short_description) onto an existing
    KB article by sys_id."""
    url = f"{_instance_url()}/api/now/table/kb_knowledge/{sys_id}"
    resp = _session().patch(url, json=fields, timeout=30)
    resp.raise_for_status()
    return True


# Service Request (sc_req_item) fields — mirrors INCIDENT_FIELDS above, but
# ServiceNow's request-item table uses `cmdb_ci` where incidents use
# `configuration_item`; callers get a normalized `configuration_item` key
# either way (see _normalize_request below).
SR_FIELDS = [
    "number", "sys_id", "short_description", "description", "close_notes",
    "state", "priority", "cmdb_ci", "assignment_group", "category",
    "opened_at", "closed_at", "sys_updated_on",
]

# sc_req_item state values: 1=Open, 2=Work in Progress, 3=Closed Complete,
# 4=Closed Incomplete, 7=Closed Skipped.
SR_CLOSED_STATE_CODES = {"3", "4", "7", "Closed Complete", "Closed Incomplete", "Closed Skipped"}


def _normalize_request(row: dict) -> dict:
    row = dict(row)
    row["configuration_item"] = row.pop("cmdb_ci", "")
    row["url"] = build_service_request_url(row.get("sys_id"))
    return row


def is_request_closed(request: dict) -> bool:
    return str((request or {}).get("state", "")).strip() in SR_CLOSED_STATE_CODES


def tool_get_service_request(number: str) -> Optional[dict]:
    """MCP tool: fetch a single service request item (sc_req_item) by number."""
    url = f"{_instance_url()}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": f"number={number}",
        "sysparm_fields": ",".join(SR_FIELDS),
        "sysparm_limit": "1",
    }
    results = _get_results(url, params)
    return _normalize_request(results[0]) if results else None


def tool_list_open_service_requests(limit: int = 200) -> list:
    """MCP tool: list active (not closed) service requests, most recently updated first."""
    url = f"{_instance_url()}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": "active=true^ORDERBYDESCsys_updated_on",
        "sysparm_fields": ",".join(SR_FIELDS),
        "sysparm_limit": str(limit),
    }
    return [_normalize_request(r) for r in _get_results(url, params)]


def tool_list_closed_service_requests(limit: int = 500, offset: int = 0) -> list:
    """MCP tool: page through Closed service requests that carry close notes."""
    url = f"{_instance_url()}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": "stateIN3,4,7^close_notesISNOTEMPTY",
        "sysparm_fields": ",".join(SR_FIELDS),
        "sysparm_limit": str(limit),
        "sysparm_offset": str(offset),
    }
    return [_normalize_request(r) for r in _get_results(url, params, timeout=60)]


def tool_update_service_request(number: str, fields: dict) -> bool:
    """MCP tool: PATCH fields (e.g. cmdb_ci, assignment_group) onto a service
    request item by number. `configuration_item` in `fields` is translated
    to ServiceNow's `cmdb_ci` column name."""
    request = tool_get_service_request(number)
    if not request or not request.get("sys_id"):
        raise ValueError(f"Service request '{number}' was not found in ServiceNow.")

    fields = dict(fields)
    if "configuration_item" in fields:
        fields["cmdb_ci"] = fields.pop("configuration_item")

    url = f"{_instance_url()}/api/now/table/sc_req_item/{request['sys_id']}"
    resp = _session().patch(url, json=fields, timeout=30)
    resp.raise_for_status()
    return True


def tool_list_assignment_groups(limit: int = 500) -> list:
    """MCP tool: list Assignment Groups (sys_user_group) — name/description."""
    url = f"{_instance_url()}/api/now/table/sys_user_group"
    params = {
        "sysparm_fields": "sys_id,name,description",
        "sysparm_limit": str(limit),
    }
    return _get_results(url, params)
