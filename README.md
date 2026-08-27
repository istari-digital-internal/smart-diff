# Smart Diff

AI-powered comparison of two Interface Control Documents (ICDs). Surfaces matches, conflicts, missing items, and an AI recommendation. Outputs a standalone HTML report with source of truth traceability.

---

## Folder Structure

```
smart-diff/
├── smart_diff.py           # main script — run this
├── system_prompt.txt       # LLM system instructions (edit to tune behavior)
├── .env                    # API keys and provider config (not for sharing)
├── README.md               # this file
├── html/
│   └── report_template.html    # HTML report template ($placeholders filled at runtime)
└── examples/
    ├── prompt_example.txt       # example user focus prompt
    ├── Warthrop_ICD_Rev3.pdf
    ├── Warthrop_SignalDefinitions.xlsx
    ├── SpecificAtomics_ICD_v2.docx
    └── SpecificAtomics_InterfaceNotes.txt
```

---

## Setup

1. Copy `.env.example` to `.env` and fill in your API key
2. Install dependencies:
```bash
pip install python-dotenv openai google-generativeai anthropic pdfplumber openpyxl python-docx
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
| `--provider` | No | `openai`, `gemini`, or `claude` (overrides .env) |
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
| `gemini` | `gemini-1.5-pro` (default), `gemini-1.5-flash`, `gemini-2.0-flash`, `gemini-2.5-pro`, `gemini-2.5-flash` |
| `claude` | `claude-opus-5` (default), `claude-opus-4-8`, `claude-opus-4-7`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-4-5` |

These lists live in the `PROVIDERS` dict at the top of `smart_diff.py` — add a model
there and it becomes valid everywhere (CLI, `.env`, `--list-models`).

```bash
python3 smart_diff.py --provider claude --model claude-sonnet-5 ...
```

---

## Output

Two files are written on each run:

- `diff_output.html` — visual diff report with SOURCE OF TRUTH TRACE, MATCHES, CONFLICTS, MISSING, AI RECOMMENDATION
- `diff_output_prompt.txt` — audit trail showing the prompt, provider, and model used

---

## .env Configuration

```
LLM_PROVIDER=openai         # openai | gemini | claude
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=
CLAUDE_API_KEY=
OPENAI_MODEL=gpt-4o         # optional overrides — see Models above for valid values
GEMINI_MODEL=gemini-1.5-pro
CLAUDE_MODEL=claude-opus-5
```
