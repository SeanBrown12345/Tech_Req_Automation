# Email notifications through Power Automate

**Admin › Notifications** emails SMEs about rows newly assigned to them, and sends reminders. ReqFill sends
those emails through a Power Automate flow: the app posts each email to the flow's web address, and
the flow sends it from the mailbox of whoever owns the flow (Outlook, so it lands in the inbox like
any colleague's mail). No mail server or IT set-up is needed.

What the app posts, once per email:

```json
{"to": "jane.doe@yourcompany.com", "subject": "12 rows to review: Acme RFP", "body": "<p>Hi Jane, ...</p>"}
```

`body` is HTML with a link to the SME's task board.

---

## 1. Create the flow

1. Go to **https://make.powerautomate.com** and sign in with your work account.
2. **+ Create** › **Instant cloud flow** › name it `ReqFill notifications` › **Skip** (don't pick a
   trigger in the wizard).
3. Add the trigger: search **When a Teams webhook request is received** (Microsoft Teams connector)
   and pick it. Set **Who can trigger the flow** to **Anyone**.
   *(The address it gives you is long and unguessable; that is what keeps it private.)*
4. **+ New step** › **Parse JSON**.
   - **Content**: pick **Body** from the trigger (in the dynamic content list).
   - **Schema**: paste
     ```json
     {
       "type": "object",
       "properties": {
         "to": {"type": "string"},
         "subject": {"type": "string"},
         "body": {"type": "string"}
       },
       "required": ["to", "subject", "body"]
     }
     ```
5. **+ New step** › **Send an email (V2)** (Office 365 Outlook).
   - **To**: `to` from Parse JSON (dynamic content). If the box wants a picked contact, choose
     **Enter custom value** first.
   - **Subject**: `subject` from Parse JSON.
   - **Body**: `body` from Parse JSON. The body is HTML, which this action sends as-is.
   - Optional, under **Show advanced options**: **From (Send as)** a shared mailbox you're allowed to
     send as, so replies don't come to you personally.
6. **Save**. Open the trigger again: it now shows the **HTTP URL**. Copy it.

If the Teams webhook trigger isn't offered, the generic **When a HTTP request is received** trigger works
the same way (paste the same schema into it and skip the Parse JSON step), but it needs a Power Automate
Premium licence.

## 2. Test the flow

In PowerShell on your PC (put your own address in `to`):

```powershell
$url = "<the HTTP URL you copied>"
$body = @{ to = "you@yourcompany.com"; subject = "ReqFill test"; body = "<p>It works.</p>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri $url -Body $body -ContentType "application/json"
```

No output means it was accepted. The email should arrive within a minute; the flow's **Run history** in
Power Automate shows each run, and why one failed.

## 3. Point ReqFill at it

**Locally**: add to `.env` in the repo root, then restart the app:

```
NOTIFY_WEBHOOK_URL=<the HTTP URL>
REQFILL_BASE_URL=http://localhost:8501
```

**Hosted**: in Azure Cloud Shell. The URL is kept as a secret, since anyone who has it can send email
through your flow:

```bash
RG=RG-CAY-RFPCC-P
APP=rfp-pilot

az containerapp secret set -g $RG -n $APP --secrets reqfill-notify-webhook="<the HTTP URL>"
az containerapp update -g $RG -n $APP -o none --set-env-vars \
  NOTIFY_WEBHOOK_URL=secretref:reqfill-notify-webhook \
  REQFILL_BASE_URL=https://rfp-pilot.yellowpond-fdf60426.canadacentral.azurecontainerapps.io/ReqFill
```

`--set-env-vars` adds or updates just these two; the app's other settings are kept. The update restarts
the app with them.

## Good to know

- Emails come from the flow owner's mailbox (or the shared mailbox picked as **From**). If that person
  leaves or their account is disabled, the flow stops: move it to a new owner (**Share** › add a co-owner)
  before then.
- Outlook connector limits are far above what ReqFill sends (one email per SME per click).
- If sending fails, ReqFill keeps those rows pending and says which emails failed, so they
  can simply be sent again.
