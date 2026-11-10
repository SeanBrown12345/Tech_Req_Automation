"""Create Microsoft Planner tasks for SMEs' reviews (Admin > Notifications), through Microsoft Graph.

One-way: the app creates tasks and never reads them back. Ported from RFP Pilot's planner_sync.py and uses
the same Azure AD app registration, so nothing new is needed in Azure. Set-up: docs/planner.md.

Sign-in is the OAuth 2.0 device-code flow (delegated): the bid manager signs in once on the Notifications
page with their Microsoft account and tasks are created as them. No client secret is involved. The sign-in
and the chosen plan are kept for the whole server in the app_settings table, so whoever opens Admin >
Notifications afterwards creates tasks as that same account until someone signs out.

  PLANNER_TENANT_ID   Azure AD tenant (default: the team's tenant, as in RFP Pilot)
  PLANNER_CLIENT_ID   the public-client app registration (default: RFP Pilot's)

Each task goes into the chosen plan, in its "Next Due" bucket (TASK_BUCKET), labelled with the client and
"Requirements" (TASK_LABEL), assigned to the SME (looked up by
their email), due on their due date, with a link to their task board.
"""

import json
import os
import re
import time
import urllib.parse

import requests

from kb import draft, settings  # noqa: F401 (settings loads .env before the variables below are read)

GRAPH = "https://graph.microsoft.com/v1.0"
# offline_access: a refresh token. Tasks.ReadWrite: create tasks, list the user's plans. User.ReadBasic.All:
# look up an SME's account from their email to assign the task (without it tasks are created unassigned).
SCOPES = "offline_access Tasks.ReadWrite User.ReadBasic.All"
TIMEOUT = 30
_EXPIRY_SKEW = 120  # refresh this many seconds before the access token runs out

# Identifiers of the team's Azure AD public-client app registration (not secrets), shared with RFP Pilot.
_DEFAULTS = {
    "tenant_id": os.environ.get("PLANNER_TENANT_ID") or "d0748657-f5a8-4d8b-8a16-b848c23ea8cb",
    "client_id": os.environ.get("PLANNER_CLIENT_ID") or "e235febb-8346-46e8-9bb9-216be78a5e8e",
    "plan_id": "",
    "plan_name": "",
}
_CONFIG_KEY, _TOKEN_KEY = "planner_config", "planner_token"


class PlannerError(Exception):
    """A failure to show the user as is (not set up, not signed in, Graph refused)."""


# ---- Config and sign-in state (app_settings) ----------------------------------------------------------

def _load(key: str) -> dict:
    try:
        value = json.loads(draft.app_setting(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def config() -> dict:
    """tenant_id, client_id, plan_id, plan_name: what's saved, over the defaults."""
    saved = _load(_CONFIG_KEY)
    return {k: (saved.get(k) or "").strip() or v for k, v in _DEFAULTS.items()}


def set_config(**values) -> dict:
    cfg = config()
    cfg.update({k: (v or "").strip() for k, v in values.items() if k in _DEFAULTS})
    draft.set_app_setting(_CONFIG_KEY, json.dumps(cfg))
    return cfg


def signed_in() -> bool:
    t = _load(_TOKEN_KEY)
    return bool(t.get("access_token") or t.get("refresh_token"))


def ready() -> bool:
    """Signed in and a plan chosen: tasks can be created."""
    return signed_in() and bool(config()["plan_id"])


def sign_out() -> None:
    draft.set_app_setting(_TOKEN_KEY, None)


def _save_tokens(token: dict, refresh_fallback: str = "") -> None:
    draft.set_app_setting(_TOKEN_KEY, json.dumps({
        "access_token": token.get("access_token", ""),
        "refresh_token": token.get("refresh_token") or refresh_fallback,
        "expires_at": int(time.time()) + int(token.get("expires_in") or 0),
    }))


def _login_url(endpoint: str) -> str:
    cfg = config()
    if not cfg["tenant_id"] or not cfg["client_id"]:
        raise PlannerError("Planner isn't set up: enter the Azure AD tenant ID and client ID in Planner settings.")
    return f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/{endpoint}"


def _post_login(endpoint: str, data: dict) -> requests.Response:
    try:
        return requests.post(_login_url(endpoint), data={"client_id": config()["client_id"], **data},
                             timeout=TIMEOUT)
    except requests.RequestException as e:
        raise PlannerError(f"Couldn't reach Microsoft sign-in (network or VPN?): {e}") from e


def _login_error(prefix: str, resp: requests.Response) -> str:
    try:
        j = resp.json()
        desc = j.get("error_description") or j.get("error") or resp.text
    except ValueError:
        desc = resp.text
    return f"{prefix}: {(desc or f'HTTP {resp.status_code}').splitlines()[0]}"


def begin_sign_in() -> dict:
    """Start a device-code sign-in: show the user `user_code` and `verification_uri`, then call
    finish_sign_in(device_code) once they've signed in."""
    resp = _post_login("devicecode", {"scope": SCOPES})
    if resp.status_code != 200:
        raise PlannerError(_login_error("Couldn't start sign-in", resp))
    return resp.json()


def finish_sign_in(device_code: str) -> bool:
    """True once the user has signed in (the tokens are saved); False while they haven't yet."""
    resp = _post_login("token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                 "device_code": device_code})
    if resp.status_code == 200:
        _save_tokens(resp.json())
        return True
    try:
        error = resp.json().get("error", "")
    except ValueError:
        error = ""
    if error in ("authorization_pending", "slow_down"):
        return False
    if error == "authorization_declined":
        raise PlannerError("Sign-in was declined.")
    if error in ("expired_token", "code_expired"):
        raise PlannerError("The sign-in code expired. Start again.")
    raise PlannerError(_login_error("Sign-in failed", resp))


def _access_token() -> str:
    t = _load(_TOKEN_KEY)
    if not (t.get("access_token") or t.get("refresh_token")):
        raise PlannerError("Not signed in to Microsoft: sign in under Planner settings.")
    if t.get("access_token") and int(t.get("expires_at", 0)) - _EXPIRY_SKEW > time.time():
        return t["access_token"]
    if not t.get("refresh_token"):
        raise PlannerError("The Microsoft sign-in expired: sign in again under Planner settings.")
    resp = _post_login("token", {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                 "scope": SCOPES})
    if resp.status_code != 200:
        sign_out()
        raise PlannerError("The Microsoft sign-in expired: sign in again under Planner settings.")
    token = resp.json()
    _save_tokens(token, refresh_fallback=t["refresh_token"])
    return token.get("access_token", "")


# ---- Graph ------------------------------------------------------------------------------------------

def _graph(method: str, path: str, *, etag: str | None = None, body: dict | None = None,
           params: dict | None = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"}
    if etag:
        headers["If-Match"] = etag
    try:
        return requests.request(method, f"{GRAPH}{path}", headers=headers, params=params, json=body,
                                timeout=TIMEOUT)
    except requests.RequestException as e:
        raise PlannerError(f"Couldn't reach Microsoft Graph (network or VPN?): {e}") from e


def _graph_error(prefix: str, resp: requests.Response) -> PlannerError:
    msg = f"HTTP {resp.status_code}"
    try:
        err = resp.json().get("error", {})
        if isinstance(err, dict) and err.get("message"):
            msg = err["message"]
    except ValueError:
        msg = resp.text[:300] or msg
    if resp.status_code in (401, 403):
        msg += " (the sign-in may have expired, or the account can't use that plan; see docs/planner.md)"
    return PlannerError(f"{prefix}: {msg}")


def list_plans() -> list[dict]:
    """[{id, title}] for the plans the signed-in user belongs to. Plans only shared with them aren't listed:
    use set_plan() with the plan's link."""
    resp = _graph("GET", "/me/planner/plans")
    if resp.status_code != 200:
        raise _graph_error("Couldn't list your plans", resp)
    return [{"id": p["id"], "title": p.get("title") or "(untitled plan)"}
            for p in resp.json().get("value", []) if p.get("id")]


def plan_id_from(text: str) -> str:
    """The plan id in a pasted plan id or Planner/Teams link ('' if there isn't one)."""
    text = (text or "").strip()
    if m := re.search(r"[?&]planId=([^&#\s]+)", text) or re.search(r"/plan/([^/?#\s]+)", text):
        return urllib.parse.unquote(m.group(1)).strip()
    return text if text and "/" not in text and " " not in text else ""


def set_plan(text: str) -> dict:
    """Check a pasted plan id or Planner link and make it the plan tasks go into. Returns {id, title}."""
    plan_id = plan_id_from(text)
    if not plan_id:
        raise PlannerError("No plan id found: paste the plan's id or a Planner link containing planId=.")
    resp = _graph("GET", f"/planner/plans/{urllib.parse.quote(plan_id)}")
    if resp.status_code != 200:
        raise _graph_error("Couldn't open that plan (check the link and that you can use it)", resp)
    plan = {"id": resp.json().get("id", plan_id), "title": resp.json().get("title") or "(untitled plan)"}
    set_config(plan_id=plan["id"], plan_name=plan["title"])
    return plan


_users: dict[str, str | None] = {}


def user_id(email: str) -> str | None:
    """The Azure AD account for a work email, or None when there's no such account."""
    email = email.strip().lower() if isinstance(email, str) else ""  # pandas gives a missing email as NaN
    if not email:
        return None
    if email not in _users:
        resp = _graph("GET", f"/users/{urllib.parse.quote(email)}", params={"$select": "id"})
        _users[email] = resp.json().get("id") if resp.status_code == 200 else None
    return _users[email]


TASK_BUCKET = "Next Due"   # every task goes in this bucket


def bucket(plan_id: str, name: str = TASK_BUCKET) -> str:
    """The id of the plan's bucket called `name`, created if it doesn't exist."""
    name = (name or TASK_BUCKET).strip()[:100]
    resp = _graph("GET", f"/planner/plans/{plan_id}/buckets")
    if resp.status_code != 200:
        raise _graph_error("Couldn't read the plan's buckets", resp)
    for b in resp.json().get("value", []):
        if str(b.get("name", "")).strip().lower() == name.lower():
            return b["id"]
    resp = _graph("POST", "/planner/buckets", body={"name": name, "planId": plan_id, "orderHint": " !"})
    if resp.status_code not in (200, 201):
        raise _graph_error(f"Couldn't create the {name!r} bucket", resp)
    return resp.json()["id"]


def _reference_key(url: str) -> str:
    """A link as a key of a task's references: Graph wants only % . : @ # escaped."""
    for ch, enc in (("%", "%25"), (".", "%2E"), (":", "%3A"), ("@", "%40"), ("#", "%23")):
        url = url.replace(ch, enc)
    return url


TASK_LABEL = "Requirements"   # every task gets this label, next to the client's name
_LABEL_SLOTS = [f"category{i}" for i in range(1, 26)]   # Planner's 25 label slots


def plan_url() -> str:
    """The address of the chosen plan's board in Planner."""
    cfg = config()
    return f"https://tasks.office.com/{cfg['tenant_id']}/Home/PlanViews/{cfg['plan_id']}"


def labels(plan_id: str, names: list[str]) -> dict:
    """The task's appliedCategories for these label names. A label the plan doesn't have yet is named into
    its first unnamed slot; one that can't be (no free slot, or the plan can't be edited) is left off."""
    names = [n.strip()[:255] for n in names if isinstance(n, str) and n.strip()]
    if not names:
        return {}
    resp = _graph("GET", f"/planner/plans/{plan_id}/details")
    if resp.status_code != 200:
        return {}
    details = resp.json()
    current = {k: (details.get("categoryDescriptions") or {}).get(k) or "" for k in _LABEL_SLOTS}
    applied, new = {}, {}
    for name in names:
        slot = next((k for k, v in {**current, **new}.items() if v.strip().lower() == name.lower()), None)
        if slot is None:
            slot = next((k for k in _LABEL_SLOTS if not current[k].strip() and k not in new), None)
            if slot is None:
                continue
            new[slot] = name
        applied[slot] = True
    if new:
        resp = _graph("PATCH", f"/planner/plans/{plan_id}/details", etag=details.get("@odata.etag", ""),
                      body={"categoryDescriptions": new})
        if resp.status_code not in (200, 204):
            applied = {k: v for k, v in applied.items() if k not in new}
    return applied


def create_task(bucket_id: str, title: str, *, assignee: str | None = None, due: str | None = None,
                description: str = "", link: tuple[str, str] | None = None,
                label_names: list[str] | None = None) -> str:
    """Create a task in the chosen plan; returns the address that opens it in Planner.
    assignee: Azure AD user id; due: YYYY-MM-DD; link: (url, label) attached to the task;
    label_names: the plan's labels (tags) to put on it, added to the plan if missing."""
    cfg = config()
    if not cfg["plan_id"]:
        raise PlannerError("No plan chosen: pick one under Planner settings.")
    body = {"planId": cfg["plan_id"], "bucketId": bucket_id, "title": title[:255]}
    if label_names and (applied := labels(cfg["plan_id"], label_names)):
        body["appliedCategories"] = applied
    if assignee:
        body["assignments"] = {assignee: {"@odata.type": "#microsoft.graph.plannerAssignment", "orderHint": " !"}}
    if due:
        body["dueDateTime"] = f"{due[:10]}T17:00:00Z"
    resp = _graph("POST", "/planner/tasks", body=body)
    if resp.status_code not in (200, 201):
        raise _graph_error("Couldn't create the task", resp)
    task_id = resp.json()["id"]
    url = f"https://tasks.office.com/{cfg['tenant_id']}/Home/Task/{task_id}"

    # The description and link live on the task's details, which need their own ETag.
    resp = _graph("GET", f"/planner/tasks/{task_id}/details")
    if resp.status_code != 200:
        return url  # the task exists; it just has no description
    etag = resp.json().get("@odata.etag", "")
    patch = {"description": description, "previewType": "description"}
    if link and link[0].startswith(("http://", "https://")):
        patch["references"] = {_reference_key(link[0]): {
            "@odata.type": "#microsoft.graph.plannerExternalReference", "alias": link[1], "type": "Other"}}
    resp = _graph("PATCH", f"/planner/tasks/{task_id}/details", etag=etag, body=patch)
    if resp.status_code not in (200, 204) and "references" in patch:
        patch.pop("references")  # the link is the fragile part: keep the description at least
        _graph("PATCH", f"/planner/tasks/{task_id}/details", etag=etag, body=patch)
    return url


def review_title(worksheet: str, client: str | None) -> str:
    """The task's title: "Review Assigned Requirement Rows: <worksheet> — <client>" (no client part when unset)."""
    client = (client or "").strip() if isinstance(client, str) else ""
    return f"Review Assigned Requirement Rows: {worksheet}" + (f" — {client}" if client else "")


def review_description(person: str, draft_name: str, sheets: dict[str, int], due_text: str | None,
                       link: str | None, assigned: bool) -> str:
    """The task's description (plain text: Planner doesn't render formatting)."""
    count = sum(sheets.values())
    lines = [f"Please review and approve the {count:,} row{'s' * (count != 1)} assigned to {person} in {draft_name}."]
    if len(sheets) > 1:
        lines += [f"  - {n:,} on {s}" for s, n in sheets.items()]
    if due_text:
        lines += ["", due_text]
    lines += ["", f"Your task board: {link}" if link else "Open ReqFill and pick your name on the task board."]
    if not assigned:
        lines += ["", f"(Not assigned in Planner: no Microsoft account was found for {person}'s email.)"]
    return "\n".join(lines)
