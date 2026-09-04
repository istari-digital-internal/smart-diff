# Smart Diff

AI-powered comparison of two Interface Control Documents (ICDs). Surfaces matches, conflicts, missing items, and an AI recommendation. Outputs a standalone HTML report with source of truth traceability.

All LLM providers are called through their REST APIs over HTTPS using `requests` — no vendor SDKs.

---

## Folder Structure

```
smart-diff/
├── smart_diff.py           # main script — run this
├── system_prompt.txt       # LLM system instructions (edit to tune behavior)
├── .env                    # API keys and provider config (not for sharing)
├── .env.example            # template for .env
├── pyproject.toml          # dependencies (poetry); dev/test and build groups separate
├── requirements.txt        # runtime dependencies (pip alternative)
├── README.md               # this file
├── html/
│   └── report_template.html    # HTML report template ($placeholders filled at runtime)
├── tests/
│   └── test_smart_diff.py      # unit tests (no network, no real document parsing)
└── examples/
    ├── prompt_example.txt       # example user focus prompt
    ├── Warthrop_ICD_Rev3.pdf
    ├── Warthrop_SignalDefinitions.xlsx
    ├── SpecificAtomics_ICD_v2.docx
    └── SpecificAtomics_InterfaceNotes.txt
```

---

## Setup

Requires Python 3.11 or 3.12.

1. Copy `.env.example` to `.env` and fill in the API key for your provider
2. Install dependencies:
```bash
poetry install            # includes the test group
# or, runtime only:
pip install -r requirements.txt
```

---

## Usage

```bash
python3 smart_diff.py \
  --prompt      "Focus on signal timing and voltage tolerances" \
  --diff-file1  examples/Warthrop_ICD_Rev3.pdf \
  --diff-file2  examples/SpecificAtomics_ICD_v2.docx
```

### All Arguments

| Argument | Required | Description |
|---|---|---|
| `--prompt` | Yes | User focus prompt for this comparison |
| `--diff-file1` | Yes | Path to Company A ICD (PDF, DOCX, XLSX, TXT) |
| `--diff-file2` | Yes | Path to Company B ICD (PDF, DOCX, XLSX, TXT) |
| `--provider` | No | `openai`, `gemini`, `claude`, or `bedrock` (overrides .env) |
| `--model` | No | Model to use — must be one of the models available for the chosen provider (overrides .env) |
| `--list-models` | No | Print the available models for each provider and exit |
| `--auth-tok` | No | LLM API key (overrides .env and `--auth-file`) |
| `--auth-file` | No | JSON file with a `token` field holding the LLM API key (overrides .env) |
| `--output` | No | Output HTML filename (default: `diff_output.html`) |

### Models

The model is resolved as `--model` → `<PROVIDER>_MODEL` in `.env` → the provider's
default (first in each list below), and is validated against the provider's list.
An unknown model exits with the valid options for that provider.

| Provider | Available models |
|---|---|
| `openai` | `gpt-4o` (default), `gpt-4o-mini`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4-turbo`, `o3`, `o4-mini` |
| `gemini` | `1.5-pro` (default), `1.5-flash`, `2.0-flash`, `2.5-pro`, `2.5-flash`, `3.6-flash` |
| `claude` | `opus-5` (default), `opus-4.8`, `opus-4.7`, `sonnet-5`, `sonnet-4.6`, `haiku-4.5` |
| `bedrock` | `opus-5` (default) |

Gemini model names are automatically prefixed with `gemini-` when the request URL
is built (pass `2.5-flash`, not `gemini-2.5-flash`).

These lists live in the `PROVIDERS` dict at the top of `smart_diff.py` — add a model
there and it becomes valid everywhere (CLI, `.env`, `--list-models`). Model lists are
pending per-environment configuration: some entries are not live at the provider
(e.g. Google has retired the `gemini-1.5` family), so confirm the model for your
deployment before relying on a default.

```bash
python3 smart_diff.py --provider gemini --model 2.5-flash ...
```

### Providers and Authentication

| Provider | Endpoint | Auth |
|---|---|---|
| `openai` | `api.openai.com` (chat completions) | `OPENAI_API_KEY` as bearer token |
| `gemini` | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` header |
| `claude` | `api.anthropic.com` (messages) | `CLAUDE_API_KEY` header |
| `bedrock` | `bedrock-runtime.<region>.amazonaws.com` (Converse) | `BEDROCK_API_KEY` as bearer token, or AWS SigV4 |

For every provider the key can instead come from `--auth-tok` or `--auth-file`
(a JSON file containing `{"token": "..."}`, e.g. an agent-delivered secret).

Bedrock specifics:

- `BEDROCK_REGION` selects the region (default `us-gov-west-1`).
- `BEDROCK_ENDPOINT` overrides the endpoint URL entirely — use it for FIPS or
  VPC interface endpoints, e.g. `https://bedrock-runtime-fips.us-gov-west-1.amazonaws.com`.
- With no bearer token, requests are signed with AWS Signature V4 from
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `AWS_SESSION_TOKEN` if set),
  implemented with the standard library only. Note: the CLI currently requires a
  non-empty token, so SigV4 applies when calling `call_llm` programmatically.

---

## Output

Two files are written on each run (the audit file sits beside `--output`):

- `diff_output.html` — visual diff report with SOURCE OF TRUTH TRACE, MATCHES, CONFLICTS, MISSING, AI RECOMMENDATION
- `diff_output_prompt.txt` — audit trail showing the prompt, provider, and model used

All model- and user-derived values are HTML-escaped when the report is built, so
document text can never render as executable markup in the report.

---

## Behavior on Failure

- One HTTPS request per run: no retries and no fallback provider or endpoint.
  A hard 300-second timeout applies to the LLM request.
- Every failure exits non-zero with a single classification-only line on stderr
  (e.g. `smart_diff: error: LLM endpoint returned HTTP status 403`). No request
  or response content and no tracebacks appear in the output.
- Nothing about the LLM exchange is logged; the only artifacts are the two
  output files above.

---

## Tests

```bash
poetry run pytest
```

The suite runs with no network access and no real document parsing: outgoing
requests are captured by a fake transport and asserted against each provider's
wire format, and the PDF/XLSX/DOCX parsers are replaced with fakes.

---

## .env Configuration

```
LLM_PROVIDER=openai         # openai | gemini | claude | bedrock
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=
CLAUDE_API_KEY=
BEDROCK_API_KEY=
OPENAI_MODEL=gpt-4o         # optional overrides — see Models above for valid values
GEMINI_MODEL=2.5-flash
CLAUDE_MODEL=opus-5
BEDROCK_MODEL=opus-5

# bedrock only
BEDROCK_REGION=us-gov-west-1
BEDROCK_ENDPOINT=           # optional FIPS/VPC endpoint override
AWS_ACCESS_KEY_ID=          # SigV4 alternative to BEDROCK_API_KEY
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
```
