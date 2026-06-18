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
  --prompt-file examples/prompt_example.txt \
  --diff-file1  examples/Warthrop_ICD_Rev3.pdf \
  --diff-file2  examples/SpecificAtomics_ICD_v2.docx \
  --file1-id   "a3f2c891-7d4e-4b1a-9f6c-2e8d5a0b3c7f Rev3" \
  --file2-id   "b7e1d452-3c8f-4a9b-8e2d-1f6a9c0d4b5e v2"
```

### All Arguments

| Argument | Required | Description |
|---|---|---|
| `--prompt-file` | Yes | Path to the user focus prompt (.txt or .docx) |
| `--diff-file1` | Yes | Path to Company A ICD (PDF, DOCX, XLSX, TXT) |
| `--diff-file2` | Yes | Path to Company B ICD (PDF, DOCX, XLSX, TXT) |
| `--file1-id` | No | Artifact UUID + revision for file1 (shown in report trace) |
| `--file2-id` | No | Artifact UUID + revision for file2 (shown in report trace) |
| `--provider` | No | `openai`, `gemini`, or `claude` (overrides .env) |
| `--auth-tok` | No | LLM API key (overrides .env) |
| `--output` | No | Output HTML filename (default: `diff_output.html`) |

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
OPENAI_MODEL=gpt-4o         # optional overrides
GEMINI_MODEL=gemini-1.5-pro
CLAUDE_MODEL=claude-opus-4-8
```
