# Azure setup: hosting ReqFill at `/ReqFill`

ReqFill runs inside RFP Pilot's Container App (`rfp-pilot`), next to RFP Pilot (`/`) and
RFPDrafter (`/RFPDrafter/`), and is served at **`<RFP Pilot URL>/ReqFill/`**. The container
build and routing live in the `Bid_Classification` repo (this repo is its `reqfill` submodule).

ReqFill needs three things the container doesn't have yet:

1. **Its own Postgres database** (`reqfill`) on RFP Pilot's existing server, for the
   knowledge base and drafts. It's passed as `KB_DATABASE_URL`, which keeps it separate from
   RFP Pilot's own `DATABASE_URL`.
2. **A file share** (`reqfill-data`) mounted at `/data/reqfill`, for uploaded worksheets
   and each draft's original workbook.
3. **More CPU and memory.** Three apps share one container now, and ReqFill loads an
   embedding model.

The app starts empty. Add worksheets on the **Add worksheet** page after it's live.

Run everything below in **Azure Cloud Shell (Bash)**, one block at a time, in the same
session (later blocks use variables from earlier ones). Every step can be safely re-run.

---

## 0. Pick the subscription and find the existing resources

```bash
az account set --subscription "<internal dev/test subscription id>"
az extension add --name containerapp --upgrade --yes

RG=RG-CAY-RFPCC-P
APP=rfp-pilot

# The Container Apps environment, the storage account RFPDrafter already uses, and
# RFP Pilot's database connection string (ReqFill reuses its server and login).
ENVN=$(basename "$(az containerapp show -g $RG -n $APP --query properties.environmentId -o tsv)")
STG=$(az containerapp env storage show -g $RG -n $ENVN --storage-name drafter-projects \
      --query properties.azureFile.accountName -o tsv)
STG_KEY=$(az storage account keys list -g $RG -n $STG --query "[0].value" -o tsv)
PILOT_URL=$(az containerapp secret show -g $RG -n $APP --secret-name db-url --query value -o tsv)
PG_HOST=$(echo "$PILOT_URL" | sed -E 's#.*@([^:/]+).*#\1#')
PG=${PG_HOST%%.*}

echo "environment=$ENVN  storage=$STG  postgres=$PG"
```

All three names should print. If `secret show` fails, list the secret names with
`az containerapp secret list -g $RG -n $APP -o table` and use the one holding the database URL.

## 1. Create ReqFill's database and store its connection string

```bash
az postgres flexible-server db create -g $RG -s $PG -d reqfill

# Same server and login as RFP Pilot, different database name.
KB_DATABASE_URL=$(echo "$PILOT_URL" | sed -E 's#/[^/?]+\?#/reqfill?#')
echo "$KB_DATABASE_URL" | sed -E 's#:[^:@/]+@#:****@#'   # should end in /reqfill?sslmode=require

az containerapp secret set -g $RG -n $APP --secrets reqfill-db-url="$KB_DATABASE_URL"
```

ReqFill creates its tables itself on first start.

## 2. Create the file share and register it with the environment

```bash
az storage share-rm create -g $RG --storage-account $STG -n reqfill-data --quota 16

az containerapp env storage set -g $RG -n $ENVN --storage-name reqfill-data \
  --azure-file-account-name $STG --azure-file-account-key "$STG_KEY" \
  --azure-file-share-name reqfill-data --access-mode ReadWrite
```

## 3. Give the container more headroom

```bash
az containerapp update -g $RG -n $APP --cpu 2.0 --memory 4.0Gi
```

Do this **before** deploying the image with ReqFill in it.

## 4. Add ReqFill's settings and mount the share

The volume mount can only be set through the app's full definition, so this exports it,
adds ReqFill's two env vars and the mount, and applies it back. Settings already on the app
are kept.

```bash
az containerapp show -g $RG -n $APP -o json > app.json

python3 - <<'PY'
import json
app = json.load(open("app.json"))
template = app["properties"]["template"]
container = template["containers"][0]

container["env"] = [e for e in container.get("env") or []
                    if e["name"] not in ("KB_DATABASE_URL", "KB_DATA_DIR")] + [
    {"name": "KB_DATABASE_URL", "secretRef": "reqfill-db-url"},
    {"name": "KB_DATA_DIR", "value": "/data/reqfill"},
]
container["volumeMounts"] = [m for m in container.get("volumeMounts") or []
                             if m["volumeName"] != "reqfill"] + [
    {"volumeName": "reqfill", "mountPath": "/data/reqfill"},
]
template["volumes"] = [v for v in template.get("volumes") or [] if v["name"] != "reqfill"] + [
    {"name": "reqfill", "storageType": "AzureFile", "storageName": "reqfill-data"},
]
json.dump(app, open("app.json", "w"), indent=2)
print("env:", [e["name"] for e in container["env"]])
print("mounts:", [m["mountPath"] for m in container["volumeMounts"]])
PY

az containerapp update -g $RG -n $APP --yaml app.json -o none   # JSON is valid YAML
```

The printout should list `KB_DATABASE_URL` and `KB_DATA_DIR` next to RFP Pilot's own
variables, and `/data/reqfill` next to RFPDrafter's mounts.

## 5. Deploy the image that includes ReqFill

Once `Bid_Classification`'s `main` has the ReqFill commit, either:

- wait for the GitHub Actions deploy that runs on the push, or
- build and roll out manually from a clone of `Bid_Classification`:
  ```bash
  bash redeploy.sh
  ```

The image build takes several minutes. It also downloads ReqFill's embedding model.

## 6. Check it's running

```bash
# ReqFill's startup lines and any errors (Ctrl+C to stop following)
az containerapp logs show -g $RG -n $APP --follow --tail 100 | grep -iE "8503|reqfill|error|traceback"

URL=$(az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv)
echo "https://$URL/ReqFill/"
```

Open that URL (the ingress is internal, so from the company network), then go to the gear
icon → **Add worksheet** and load your completed worksheets.

---

## Later: shipping ReqFill changes

Pushing this repo's `main` deploys nothing by itself. In `Bid_Classification`:

```bash
git submodule update --remote reqfill
git add reqfill && git commit -m "Bump reqfill submodule: <summary>"
git push origin main        # triggers the deploy (or run: bash redeploy.sh)
```

**Database schema changes:** hosted, ReqFill never rebuilds its tables on its own. If a
change bumps `SCHEMA_VERSION` in `kb/store.py`, the app stops with a "migrate the Postgres
tables" error until the tables are migrated.
