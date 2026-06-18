import argparse, os, json
from pathlib import Path
from string import Template
from datetime import datetime
from dotenv import load_dotenv

# ── .env configuration ────────────────────────────────────────────────────────
# Settings and API keys are read from the .env file in the same directory.
#
#   LLM_PROVIDER   = openai | gemini | claude      (which backend to use)
#   OPENAI_API_KEY = sk-...                         (required if provider=openai)
#   GEMINI_API_KEY = ...                            (required if provider=gemini)
#   CLAUDE_API_KEY = ...                            (required if provider=claude)
#   OPENAI_MODEL   = gpt-4o                         (optional — shown above is default)
#   GEMINI_MODEL   = gemini-1.5-pro                 (optional — shown above is default)
#   CLAUDE_MODEL   = claude-opus-4-8                (optional — shown above is default)
#
# CLI flags --provider and --auth-tok override .env values if provided.
# ─────────────────────────────────────────────────────────────────────────────

def read_file(p):
    # Extracts plain text from PDF, XLSX, DOCX, or any plain-text file.
    ext = Path(p).suffix.lower()
    if ext == '.pdf':
        import pdfplumber
        return '\n'.join(pg.extract_text() or '' for pg in pdfplumber.open(p).pages)
    if ext == '.xlsx':
        import openpyxl
        wb = openpyxl.load_workbook(p, data_only=True)          # data_only=True returns cell values, not formulas
        return '\n'.join('  |  '.join(str(c) for c in row if c is not None)
                         for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
    if ext == '.docx':
        from docx import Document
        return '\n'.join(para.text for para in Document(p).paragraphs if para.text.strip())
    return Path(p).read_text(errors='replace')                  # fallback: plain text (.txt, .csv, etc.)

def call_llm(provider, token, model, system, msg):
    # Calls the chosen LLM and returns the response text.
    # provider = set by --provider CLI arg or LLM_PROVIDER in .env  (e.g. 'openai', 'gemini', 'claude')
    # token    = set by --auth-tok CLI arg or OPENAI_API_KEY / GEMINI_API_KEY / CLAUDE_API_KEY in .env
    # model    = set by OPENAI_MODEL / GEMINI_MODEL / CLAUDE_MODEL in .env (defaults: gpt-4o, gemini-1.5-pro, claude-opus-4-8)
    # system   = standing instructions loaded from system_prompt.txt
    # msg      = user prompt — the two file contents + user's focus for this specific run
    if provider == 'openai':
        from openai import OpenAI
        return OpenAI(api_key=token).chat.completions.create(
            model=model,
            messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': msg}]
        ).choices[0].message.content
    if provider == 'gemini':
        import google.generativeai as genai
        genai.configure(api_key=token)                          # key must be set before model init
        return genai.GenerativeModel(model, system_instruction=system).generate_content(msg).text
    if provider == 'claude':
        import anthropic
        return anthropic.Anthropic(api_key=token).messages.create(
            model=model, max_tokens=4096, system=system,
            messages=[{'role': 'user', 'content': msg}]
        ).content[0].text

def main():
    load_dotenv()

    # CLI arguments
    ap = argparse.ArgumentParser()
    ap.add_argument('--prompt-file', required=True)                   # path to static prompt .txt/.docx file
    ap.add_argument('--diff-file1',  required=True)                   # Company A ICD
    ap.add_argument('--diff-file2',  required=True)                   # Company B ICD
    ap.add_argument('--auth-tok',    default=None)                    # LLM API key (overrides .env)
    ap.add_argument('--provider',    default=None)                    # openai | gemini | claude (overrides .env)
    ap.add_argument('--file1-uuid',  default=None)                    # Istari artifact UUID for Company A
    ap.add_argument('--file1-rev',   default=None)                    # Istari revision ID for Company A
    ap.add_argument('--file2-uuid',  default=None)                    # Istari artifact UUID for Company B
    ap.add_argument('--file2-rev',   default=None)                    # Istari revision ID for Company B
    ap.add_argument('--output',      default='diff_output.html')      # output HTML filename
    args = ap.parse_args()

    # probably better ways to do this but i think this works for now gerhard lmk what you think
    # single dict maps provider name to its .env key and default model
    # required in .env: whichever API key matches your provider (e.g. OPENAI_API_KEY if using openai)
    # optional in .env: LLM_PROVIDER (defaults to openai), OPENAI_MODEL, GEMINI_MODEL, CLAUDE_MODEL
    PROVIDERS = {
        'openai': ('OPENAI_API_KEY', os.getenv('OPENAI_MODEL', 'gpt-4o')),         # .env: OPENAI_MODEL
        'gemini': ('GEMINI_API_KEY', os.getenv('GEMINI_MODEL', 'gemini-1.5-pro')), # .env: GEMINI_MODEL
        'claude': ('CLAUDE_API_KEY', os.getenv('CLAUDE_MODEL', 'claude-opus-4-8')),# .env: CLAUDE_MODEL
    }
    provider       = args.provider or os.getenv('LLM_PROVIDER', 'openai')  # .env: LLM_PROVIDER
    env_key, model = PROVIDERS[provider]                                    # unpack key name + model for chosen provider
    token          = args.auth_tok or os.getenv(env_key, '')                # .env: OPENAI_API_KEY / GEMINI_API_KEY / CLAUDE_API_KEY

    # Read inputs
    system    = (Path(__file__).parent / 'system_prompt.txt').read_text().strip()
    prompt    = Path(args.prompt_file).read_text().strip()
    filename1, filename2 = Path(args.diff_file1).name, Path(args.diff_file2).name  # filenames used as report labels
    uuid1, rev1 = (args.file1_uuid or ''), (args.file1_rev or '')  # Istari artifact UUID and revision for Company A
    uuid2, rev2 = (args.file2_uuid or ''), (args.file2_rev or '')  # Istari artifact UUID and revision for Company B
    f1, f2    = read_file(args.diff_file1), read_file(args.diff_file2)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # send both files + the user's focus to the LLM
    raw  = call_llm(provider, token, model, system, f"""Return ONLY valid JSON in this exact format:
{{"matches":["..."],"conflicts":[{{"item":"","value1":"","value2":""}}],"missing":[{{"item":"","missing_from":"","detail":""}}],"recommendation":"..."}}

User focus: {prompt}

--- Document 1 | {filename1} | UUID: {uuid1} | Revision: {rev1} ---
{f1}

--- Document 2 | {filename2} | UUID: {uuid2} | Revision: {rev2} ---
{f2}""")
    diff = json.loads(raw.strip().lstrip('`json\n').rstrip('`'))  # strip backticks the LLM sometimes adds around JSON

    # Build HTML report and write both output files
    out = Path(args.output)
    out.write_text(Template(
        (Path(__file__).parent / 'html' / 'report_template.html')
        .read_text()).substitute(
        filename1=filename1, filename2=filename2,
        uuid1=uuid1, rev1=rev1, uuid2=uuid2, rev2=rev2,
        provider=provider, model=model, timestamp=timestamp,
        matches_html  = ''.join(f'<li>{m}</li>' for m in diff['matches']),
        conflicts_html= ''.join(
                        f'<tr style="border-bottom:1px solid #ddd">'
                        f'<td style="padding:8px">{c["item"]}</td>'
                        f'<td style="padding:8px">{c["value1"]}</td>'
                        f'<td style="padding:8px">{c["value2"]}</td></tr>'
                        for c in diff['conflicts']),
        missing_html  = ''.join(
                        f'<li><b>{m["missing_from"]}</b> did not specify {m["item"]}. {m.get("detail","")}</li>'
                        for m in diff['missing']),
        recommendation= diff['recommendation'],
    ))
    Path(out.stem + '_prompt.txt').write_text(f'PROMPT\n{"="*40}\n{prompt}\n\nPROVIDER: {provider}\nMODEL: {model}\n')
    print(f'Done — {out} + {out.stem}_prompt.txt')

    # i am not as familiar with building html from python, comments below are for my own self awareness and can be deleted
    # out = Path(args.output)                          — sets the output file path from the --output arg
    # Template(...).read_text()                        — loads report_template.html from the html/ folder next to this script
    # .substitute(...)                                 — swaps every $placeholder in the template with real data
    # filename1/filename2                              — the two filenames shown in the source of truth trace box at the top
    # uuid1/rev1/uuid2/rev2                            — the istari artifact UUIDs and revision IDs for traceability
    # provider/model/timestamp                         — which llm ran the diff and when it ran
    # matches_html                                     — builds a <li> bullet for each match the llm found
    # conflicts_html                                   — builds a <tr> table row for each conflict: item / value from file1 / value from file2
    # missing_html                                     — builds a <li> bullet for each item one doc is missing
    # recommendation                                   — drops the llm recommendation in as plain text
    # Path(out.stem + '_prompt.txt').write_text(...)   — writes a second file next to the html with the prompt + model used as an audit trail

if __name__ == '__main__':
    main()
