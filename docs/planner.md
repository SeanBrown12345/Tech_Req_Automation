# Microsoft Planner tasks

**Admin › Notifications** can create a Microsoft Planner task for each SME's review of a draft, as
well as emailing them. It's one-way: ReqFill creates the tasks and never reads them back.

ReqFill uses the same Azure AD app registration as RFP Pilot (see RFP Pilot's `PLANNER_SETUP.md`), so
if Planner works in RFP Pilot, nothing else is needed in Azure. To use a different registration, set
`PLANNER_TENANT_ID` and `PLANNER_CLIENT_ID`.

## Connect (once per server)

1. Open **Admin › Notifications › Planner settings** and click **Sign in to Microsoft**.
2. Go to the address shown (`https://microsoft.com/devicelogin`), enter the code, and sign in with
   your work account. Back in ReqFill, click **I've signed in**.
3. Click **Load my plans**, pick the plan, and click **Use this plan**. If the plan is only shared with
   you and isn't listed, paste its Planner link (one containing `planId=`) instead.

The sign-in and plan are saved for the whole server, not for one browser. Tasks are created as the
account that signed in until someone clicks **Sign out**, and that account must be a member of the plan.

## Creating tasks

Tick lines, or tick none to use every line shown, then click **Create Planner tasks**. Each line
(one SME on one draft) gets a task:

- **Bucket**: **Next Due**. It's created if the plan doesn't have one.
- **Labels**: the draft's client (e.g. `Anchorage`) and `Requirements`. A label the plan doesn't have yet
  is named into its first unnamed label slot; if all 25 slots are named, that label is left off.
- **Title**: `Review Assigned Requirement Rows: <worksheet> — <client>`
- **Description**: starts `Please review and approve the 12 rows assigned to <SME> in <worksheet> — <client>.`
- **Assigned to**: the SME, found from their email under Admin › SMEs. If there's no email, or no
  Microsoft account matches it, the task is created unassigned and the description says so.
- **Due**: the line's **Due** date, at 17:00 UTC.
- **Description**: the rows per sheet, the due date and a link to the SME's task board. A link is
  attached too when `REQFILL_BASE_URL` is set.

The grid's **Planner** column opens the task last created for each line. Lines that already have a task
are skipped unless you tick **Also for the lines that already have a task**. Creating a task doesn't
count as emailing: the **New** count and **Last emailed** don't change.

## Troubleshooting

- **401 / 403**: the sign-in expired, or the signed-in account isn't a member of the plan. Sign in again,
  or pick a plan that account belongs to.
- **Tasks are unassigned**: the SME has no email under Admin › SMEs, or the email isn't their Microsoft
  sign-in address.
- **"public client flows" error at sign-in**: the app registration needs *Allow public client flows*
  turned on (RFP Pilot's `PLANNER_SETUP.md`, step 6).
