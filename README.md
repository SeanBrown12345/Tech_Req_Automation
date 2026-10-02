# Tech_Req_Automation

Knowledge base of past functional/technical requirements worksheet responses, used to draft answers for new RFPs.

## Dashboard

```
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

- **Overview** - totals, answers by status and module, loaded worksheets, and *conflicting answers*
  (near-identical requirements answered differently in different RFPs).
- **Search** - paste a requirement to find how we've answered similar ones (hybrid / semantic / keyword),
  filtered by status, client and module.
- **Add worksheet** - upload a completed worksheet; the header row, columns and answer scale are detected
  and suggested, you confirm, preview, and load. Added worksheets can be removed from the same page.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `KB_DATA_DIR` | `./data` | Database, uploaded workbooks, dashboard-created profiles. On Azure, mount persistent storage here. |
| `KB_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model (vectors are cached per model, so switching is safe) |
| `KB_MODEL_CACHE` | `./models` | Where model files are downloaded; bake into the image for Azure |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | - | Matcha gateway access for Claude (drafting, coming next). Put them in `.env` locally. |

## Loader (command line)

```
pip install -r requirements.txt
python -m kb ingest                 # load every workbook in config/profiles/
python -m kb ingest config/profiles/dayton.yaml
python -m kb stats
python -m kb embed                  # embed any rows without vectors (ingest does this automatically)
python -m kb search "single sign on" --status STANDARD
python -m kb search "vendor-hosted cloud" --mode keyword   # or semantic; default is hybrid
python -m kb export data/records.jsonl
```

- `config/profiles/*.yaml` - one per workbook: sheets, header row, column roles, how the answer is encoded.
- `config/scales.yaml` - maps each worksheet's response vocabulary onto the internal statuses in `kb/statuses.py`.
- `data/kb.sqlite` - generated store (records, FTS5 keyword index, embedding cache). Safe to delete and rebuild.

### Search
Hybrid search merges keyword (BM25) and semantic (embedding) rankings with reciprocal rank fusion.
Embeddings run locally on CPU via `fastembed` (ONNX) - no content leaves the machine.

### Adding a workbook
Easiest: use the dashboard's **Add worksheet** page. To add one by hand instead:
1. Copy the closest existing profile, set `file`, `source`, sheet names, `header_row` and column letters.
2. Point each answer at an existing scale, or add a new scale to `config/scales.yaml`.
3. Run `python -m kb ingest <profile>`; any `! unmapped value` lines tell you what to add to the scale.
