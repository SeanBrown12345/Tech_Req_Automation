# Tech_Req_Automation

Knowledge base of past functional/technical requirements worksheet responses, used to draft answers for new RFPs.

## Loader

```
pip install -r requirements.txt
python -m kb ingest                 # load every workbook in config/profiles/
python -m kb ingest config/profiles/dayton.yaml
python -m kb stats
python -m kb search "single sign on" --status STANDARD
python -m kb export data/records.jsonl
```

- `config/profiles/*.yaml` - one per workbook: sheets, header row, column roles, how the answer is encoded.
- `config/scales.yaml` - maps each worksheet's response vocabulary onto the internal statuses in `kb/statuses.py`.
- `data/kb.sqlite` - generated store (records + FTS5 keyword index). Safe to delete and rebuild.

### Adding a workbook
1. Copy the closest existing profile, set `file`, `source`, sheet names, `header_row` and column letters.
2. Point each answer at an existing scale, or add a new scale to `config/scales.yaml`.
3. Run `python -m kb ingest <profile>`; any `! unmapped value` lines tell you what to add to the scale.
