# Smart Diff

AI-powered comparison of two extracted document artifacts (ICDs). Surfaces matches, conflicts, missing items, and an AI recommendation. Outputs a standalone HTML report with source of truth traceability.

Runtime dependencies are `python-dotenv`, `requests`, and the Python standard library only. No LLM vendor SDKs, no AWS SDK, no document parsers. Document parsing is performed upstream by the platform extraction modules; Smart Diff consumes their plain-text output.

---

## Folder Structure

```
smart-diff/
├── smart_diff.py           # main script — run this
├── system_prompt.txt       # LLM system instructions (edit to tune behavior)
├── .env                    # endpoint and auth config (not for sharing)
├── .env.example            # template for .env
├── README.md               # this file
├── pyproject.toml          # pinned dependencies (poetry)
├── poetry.lock             # exact resolved versions (matches the approved list)
├── html/
│   └── report_template.html    # HTML report template ($placeholders filled at runtime)
└── examples/                    # fake demonstration ICDs
```

---

## Setup

```bash
poetry install              # runtime deps only
poetry install --with build # add pyinstaller/poethepoet for building the executable
```

Copy `.env.example` to `.env` and fill in the provider settings.

---

## Usage

Inputs are extraction products (plain text, HTML, or JSON from platform extract jobs), not raw PDF/DOCX/XLSX files.

```bash
python3 smart_diff.py \
  --prompt     "Compare these two ICDs for bus integration conflicts." \
  --diff-file1 text_a.txt \
  --diff-file2 text_b.txt \
  --file1-uuid <uuid> --file1-rev <rev> \
  --file2-uuid <uuid> --file2-rev <rev>
```

### All Arguments

| Argument | Required | Description |
|---|---|---|
| `--prompt` | Yes | User focus prompt as text |
| `--diff-file1` | Yes | Extracted artifact for side A |
| `--diff-file2` | Yes | Extracted artifact for side B |
| `--auth-tok` | No | LLM bearer token (overrides .env) |
| `--provider` | No | `bedrock` or `openai_compat` (overrides .env) |
| `--file1-uuid` / `--file1-rev` | No | Platform UUID and revision for side A (shown in report trace) |
| `--file2-uuid` / `--file2-rev` | No | Platform UUID and revision for side B (shown in report trace) |
| `--output` | No | Output HTML filename (default `diff_output.html`) |

---

## Providers

| Provider | Endpoint | Auth |
|---|---|---|
| `bedrock` | AWS Bedrock Converse API (`BEDROCK_ENDPOINT` / `BEDROCK_REGION`, FIPS endpoints supported) | Bearer token, or AWS SigV4 via standard AWS environment variables |
| `openai_compat` | Any chat-completions endpoint (`LLM_BASE_URL`) | Bearer token |

Model temperature is fixed at 0. LLM responses are validated against the expected JSON schema; one retry is attempted, after which the run fails cleanly.

---

## Output

- `diff_output.html` — visual diff report with SOURCE OF TRUTH TRACE, MATCHES, CONFLICTS, MISSING, AI RECOMMENDATION
- `diff_output_prompt.txt` — audit trail showing the prompt, provider, and model used

---

## Build (executable)

```bash
poetry run poe build
```

Produces a single self-contained executable via PyInstaller with `system_prompt.txt` and the report template bundled.
