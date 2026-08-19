import argparse, os, json
import hashlib, hmac, urllib.parse
from pathlib import Path
from string import Template
from datetime import datetime, timezone
from dotenv import load_dotenv
import requests

REQUEST_TIMEOUT_S = 300

# ── .env configuration ────────────────────────────────────────────────────────
# Settings and API keys are read from the .env file in the same directory.
# All providers are called through their REST APIs via requests; no vendor
# SDKs are used. bedrock and genai_mil are the government paths and have no
# default endpoints or models.
#
#   LLM_PROVIDER   = openai | gemini | claude | bedrock | genai_mil
#
#   openai:    OPENAI_API_KEY,  OPENAI_MODEL  (default gpt-4o)
#   gemini:    GEMINI_API_KEY,  GEMINI_MODEL  (default gemini-1.5-pro)
#   claude:    CLAUDE_API_KEY,  CLAUDE_MODEL  (default claude-opus-4-8)
#   bedrock:   LLM_AUTH_TOKEN or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (SigV4),
#              BEDROCK_MODEL_ID (required), BEDROCK_REGION (default us-gov-west-1),
#              BEDROCK_ENDPOINT (optional, e.g. FIPS or VPC endpoint)
#   genai_mil: LLM_AUTH_TOKEN, LLM_BASE_URL (required), LLM_MODEL (required)
#
# CLI flags --provider, --auth-tok, and --auth-file override .env values.
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

def _sigv4_headers(url, region, payload, access_key, secret_key, session_token=None):
    # AWS Signature Version 4 for a single POST request, standard library only.
    parsed = urllib.parse.urlparse(url)
    now = datetime.now(timezone.utc)
    amz_date, date_stamp = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(payload.encode()).hexdigest()
    headers = {'host': parsed.netloc, 'x-amz-date': amz_date, 'x-amz-content-sha256': payload_hash}
    if session_token:
        headers['x-amz-security-token'] = session_token
    signed = ';'.join(sorted(headers))
    canonical = '\n'.join(['POST', urllib.parse.quote(parsed.path or '/'), '',
                           ''.join(f'{k}:{headers[k]}\n' for k in sorted(headers)), signed, payload_hash])
    scope = f'{date_stamp}/{region}/bedrock/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    key = ('AWS4' + secret_key).encode()
    for part in (date_stamp, region, 'bedrock', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {'X-Amz-Date': amz_date, 'X-Amz-Content-Sha256': payload_hash,
           'Authorization': f'AWS4-HMAC-SHA256 Credential={access_key}/{scope}, '
                            f'SignedHeaders={signed}, Signature={sig}'}
    if session_token:
        out['X-Amz-Security-Token'] = session_token
    return out

def _chat_completions(base, token, model, system, msg):
    # OpenAI-compatible chat completions shape (openai and genai_mil).
    payload = json.dumps({'model': model, 'temperature': 0,
                          'messages': [{'role': 'system', 'content': system},
                                       {'role': 'user', 'content': msg}]})
    resp = requests.post(f'{base.rstrip("/")}/chat/completions', data=payload,
                         headers={'Content-Type': 'application/json',
                                  'Authorization': f'Bearer {token}'},
                         timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content']

def call_llm(provider, token, model, system, msg):
    # One HTTPS POST to the chosen model's REST API, temperature 0 for
    # deterministic output. No vendor SDKs; requests only.
    if provider == 'openai':
        return _chat_completions('https://api.openai.com/v1', token, model, system, msg)

    if provider == 'genai_mil':
        base = os.getenv('LLM_BASE_URL', '')
        if not base:
            raise SystemExit('LLM_BASE_URL is not set. Configure the GenAI.mil endpoint; '
                             'there is no default for this provider.')
        return _chat_completions(base, token, model, system, msg)

    if provider == 'gemini':
        url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
               f'{urllib.parse.quote(model)}:generateContent')
        payload = json.dumps({'system_instruction': {'parts': [{'text': system}]},
                              'contents': [{'role': 'user', 'parts': [{'text': msg}]}],
                              'generationConfig': {'temperature': 0}})
        resp = requests.post(url, data=payload,
                             headers={'Content-Type': 'application/json',
                                      'x-goog-api-key': token},
                             timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return ''.join(p.get('text', '')
                       for p in resp.json()['candidates'][0]['content']['parts'])

    if provider == 'claude':
        payload = json.dumps({'model': model, 'max_tokens': 8192, 'temperature': 0,
                              'system': system,
                              'messages': [{'role': 'user', 'content': msg}]})
        resp = requests.post('https://api.anthropic.com/v1/messages', data=payload,
                             headers={'Content-Type': 'application/json',
                                      'x-api-key': token,
                                      'anthropic-version': '2023-06-01'},
                             timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return ''.join(b.get('text', '') for b in resp.json()['content'])

    if provider == 'bedrock':
        region = os.getenv('BEDROCK_REGION', 'us-gov-west-1')
        base = os.getenv('BEDROCK_ENDPOINT', f'https://bedrock-runtime.{region}.amazonaws.com').rstrip('/')
        url = f'{base}/model/{urllib.parse.quote(model, safe="")}/converse'
        payload = json.dumps({'system': [{'text': system}],
                              'messages': [{'role': 'user', 'content': [{'text': msg}]}],
                              'inferenceConfig': {'temperature': 0, 'maxTokens': 8192}})
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'                 # Bedrock API key
        else:
            headers.update(_sigv4_headers(url, region, payload,          # or AWS role credentials
                os.getenv('AWS_ACCESS_KEY_ID', ''), os.getenv('AWS_SECRET_ACCESS_KEY', ''),
                os.getenv('AWS_SESSION_TOKEN') or None))
        resp = requests.post(url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return ''.join(b.get('text', '') for b in resp.json()['output']['message']['content'])

    raise SystemExit(f"Unknown provider '{provider}'. "
                     "Options: openai, gemini, claude, bedrock, genai_mil.")

def main():
    load_dotenv()

    # CLI arguments
    ap = argparse.ArgumentParser()
    ap.add_argument('--prompt', required=True)                        # Textual prompt
    ap.add_argument('--diff-file1',  required=True)                   # Company A ICD
    ap.add_argument('--diff-file2',  required=True)                   # Company B ICD
    ap.add_argument('--auth-tok',    default=None)                    # LLM API key (overrides .env and --auth-file)
    ap.add_argument('--auth-file',   default=None)                    # LLM API key file (overrides .env)
    ap.add_argument('--provider',    default=None)                    # openai | gemini | claude | bedrock | genai_mil (overrides .env)
    ap.add_argument('--output',      default='diff_output.html')      # output HTML filename
    args = ap.parse_args()

    # Maps each provider to its .env token key and its model setting. The
    # commercial three keep their original keys and defaults so existing
    # environments work unchanged; the government two require explicit config.
    PROVIDERS = {
        'openai':    ('OPENAI_API_KEY', os.getenv('OPENAI_MODEL', 'gpt-4o')),
        'gemini':    ('GEMINI_API_KEY', os.getenv('GEMINI_MODEL', 'gemini-1.5-pro')),
        'claude':    ('CLAUDE_API_KEY', os.getenv('CLAUDE_MODEL', 'claude-opus-4-8')),
        'bedrock':   ('LLM_AUTH_TOKEN', os.getenv('BEDROCK_MODEL_ID', '')),
        'genai_mil': ('LLM_AUTH_TOKEN', os.getenv('LLM_MODEL', '')),
    }
    provider = args.provider or os.getenv('LLM_PROVIDER', 'openai')  # .env: LLM_PROVIDER
    if provider not in PROVIDERS:
        raise SystemExit(f"Unknown provider '{provider}'. "
                         "Options: openai, gemini, claude, bedrock, genai_mil.")
    env_key, model = PROVIDERS[provider]
    if not model:
        raise SystemExit(f"No model configured for provider '{provider}'. "
                         "Set BEDROCK_MODEL_ID or LLM_MODEL in .env.")
    if args.auth_tok:
        token = args.auth_tok
    elif args.auth_file:
        tok_json = json.loads(Path(args.auth_file).read_text())
        token = tok_json['token']
    else:
        token = os.getenv(env_key, '')  # per-provider .env key; empty means SigV4 creds for bedrock

    # Read inputs
    system    = (Path(__file__).parent / 'system_prompt.txt').read_text().strip()
    prompt    = args.prompt.strip()
    filename1, filename2 = Path(args.diff_file1).name, Path(args.diff_file2).name  # filenames used as report labels
    f1, f2    = read_file(args.diff_file1), read_file(args.diff_file2)
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # send both files + the user's focus to the LLM
    raw  = call_llm(provider, token, model, system, f"""Return ONLY valid JSON in this exact format:
{{"matches":["..."],"conflicts":[{{"item":"","value1":"","value2":""}}],"missing":[{{"item":"","missing_from":"","detail":""}}],"recommendation":"..."}}

User focus: {prompt}

--- Document 1 | {filename1} ---
{f1}

--- Document 2 | {filename2} ---
{f2}""")
    diff = json.loads(raw.strip().lstrip('`json\n').rstrip('`'))  # strip backticks the LLM sometimes adds around JSON

    # Build HTML report and write both output files
    out = Path(args.output)
    out.write_text(Template(
        (Path(__file__).parent / 'html' / 'report_template.html')
        .read_text()).substitute(
        filename1=filename1, filename2=filename2,
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
