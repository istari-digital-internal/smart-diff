import hashlib
import hmac
import argparse, os, json
from pathlib import Path
from string import Template
from datetime import datetime, timezone
import requests
import urllib.parse
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
#   CLAUDE_MODEL   = claude-opus-5                  (optional — shown above is default)
#
# CLI flags --provider, --model and --auth-tok override .env values if provided.
# ─────────────────────────────────────────────────────────────────────────────

# ── Provider registry ─────────────────────────────────────────────────────────
# Single source of truth for every backend this script can talk to. Each entry:
#   env_key   — .env variable holding that provider's API key
#   env_model — .env variable that can override the model without a CLI flag
#   default   — model used when neither --model nor env_model is set
#   models    — the only models accepted for this provider; --model is validated
#               against this tuple, first entry is the default
# Adding a new model is a one-line change here; nothing else needs to know.
# TODO: Configure models per environment/deployment. Some are unsupported in current LLM provider versions, like gemini-1.5-pro
PROVIDERS = {
    'openai': {
        'base_url': 'https://api.openai.com/v1',
        'env_key':   'OPENAI_API_KEY',
        'env_model': 'OPENAI_MODEL',
        'models':    ('gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'gpt-4.1-mini',
                      'gpt-4-turbo', 'o3', 'o4-mini'),
    },
    'gemini': {
        'base_url': 'https://generativelanguage.googleapis.com/v1beta/models/',
        'env_key':   'GEMINI_API_KEY',
        'env_model': 'GEMINI_MODEL',
        'models':    ('1.5-pro', '1.5-flash', '2.0-flash',
                      '2.5-pro', '2.5-flash', '3.6-flash'),
    },
    'claude': {
        'base_url': 'https://api.anthropic.com/v1/messages',
        'env_key':   'CLAUDE_API_KEY',
        'env_model': 'CLAUDE_MODEL',
        'models':    ('opus-5', 'opus-4.8', 'opus-4.7',
                      'sonnet-5', 'sonnet-4.6', 'haiku-4.5'),
    },
    'bedrock': {
        'base_url': 'https://bedrock-runtime.{region}.amazonaws.com',
        'env_key':   'BEDROCK_API_KEY',
        'env_model': 'BEDROCK_MODEL',
        'models':    ('opus-5',),
    },
}

REQUEST_TIMEOUT_S = 300

def default_model(provider):
    # First model listed for a provider is its default.
    return PROVIDERS[provider]['models'][0]

def resolve_model(provider, cli_model):
    # Picks the model and validates it against the provider's allowed list.
    # Precedence: --model  >  <PROVIDER>_MODEL in .env  >  provider default.
    # Returns (model, error_or_None) — the caller turns the error into a clean exit.
    cfg = PROVIDERS[provider]
    env_model = os.getenv(cfg['env_model'])
    if cli_model:
        model, source = cli_model, '--model'
    elif env_model:
        model, source = env_model, f"{cfg['env_model']} in .env"
    else:
        return default_model(provider), None                 # default is trusted, no check needed
    if model not in cfg['models']:
        # Name the source so a stale .env is not mistaken for a bad flag.
        return None, (f"invalid model {model!r} for provider {provider!r} (set via {source}).\n"
                      f"  valid models: {', '.join(cfg['models'])}")
    return model, None

def models_table():
    # Human-readable dump of the registry for --list-models.
    return '\n'.join(
        f"{p}:\n" + '\n'.join(f"    {m}" + ('  (default)' if i == 0 else '')
                              for i, m in enumerate(cfg['models']))
        for p, cfg in PROVIDERS.items())

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
    # OpenAI-compatible chat completions shape.
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
    # Calls the chosen LLM and returns the response text.
    # provider = set by --provider CLI arg or LLM_PROVIDER in .env  (e.g. 'openai', 'gemini', 'claude')
    # token    = set by --auth-tok CLI arg or OPENAI_API_KEY / GEMINI_API_KEY / CLAUDE_API_KEY in .env
    # model    = set by --model CLI arg, else OPENAI_MODEL / GEMINI_MODEL / CLAUDE_MODEL in .env,
    #            else the provider default — already validated against PROVIDERS by resolve_model()
    # system   = standing instructions loaded from system_prompt.txt
    # msg      = user prompt — the two file contents + user's focus for this specific run
    if provider not in PROVIDERS:
        raise SystemExit(f'error: no config provided for {provider!r}')
    
    base_url = PROVIDERS[provider]['base_url']

    if provider == 'openai':
        return _chat_completions(base_url, token, model, system, msg)
    
    if provider == 'gemini':
        # Prepend 'gemini-' to format model name for URL (the gemini SDK handled this previously)
        model = f'gemini-{model}'
        url = (f'{base_url}'
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
        payload = json.dumps({'model': model, 'max_tokens': 16000, 'temperature': 0,
                              'system': system,
                              'messages': [{'role': 'user', 'content': msg}]})
        resp = requests.post(base_url, data=payload,
                             headers={'Content-Type': 'application/json',
                                      'x-api-key': token,
                                      'anthropic-version': '2023-06-01'},
                             timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return ''.join(b.get('text', '') for b in resp.json()['content'])

    if provider == 'bedrock':
        
        # Load AWS config from environment
        region = os.getenv('BEDROCK_REGION', 'us-gov-west-1')
        aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID', '')
        aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY', '')
        aws_session_token = os.getenv('AWS_SESSION_TOKEN')

        # BEDROCK_ENDPOINT (e.g. FIPS or VPC endpoint) overrides the regional default.
        base_url = os.getenv('BEDROCK_ENDPOINT', base_url.format(region=region)).rstrip('/')
        url = f'{base_url}/model/{urllib.parse.quote(model, safe="")}/converse'
        payload = json.dumps({'system': [{'text': system}],
                              'messages': [{'role': 'user', 'content': [{'text': msg}]}],
                              'inferenceConfig': {'temperature': 0, 'maxTokens': 16000}})
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'                 # Bedrock API key
        else:
            headers.update(_sigv4_headers(url, region, payload,          # or AWS role credentials
                aws_access_key_id, aws_secret_access_key,
                aws_session_token or None))
        resp = requests.post(url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT_S)
        resp.raise_for_status()
        return ''.join(b.get('text', '') for b in resp.json()['output']['message']['content'])

    raise SystemExit(f'error: unsupported provider {provider!r}')

def main():
    load_dotenv()

    # CLI arguments
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='available models per provider:\n' + models_table())
    ap.add_argument('--prompt', required=True)                        # Textual prompt
    ap.add_argument('--diff-file1',  required=True)                   # Company A ICD
    ap.add_argument('--diff-file2',  required=True)                   # Company B ICD
    ap.add_argument('--auth-tok',    default=None)                    # LLM API key (overrides .env and --auth-file)
    ap.add_argument('--auth-file',   default=None)                    # LLM API key file (overrides .env)
    ap.add_argument('--provider',    default=None,                    # overrides LLM_PROVIDER in .env
                    choices=sorted(PROVIDERS))                        # argparse rejects anything else with usage text
    ap.add_argument('--model',       default=None,                    # overrides <PROVIDER>_MODEL in .env
                    metavar='MODEL')                                  # validated below — valid set depends on --provider
    ap.add_argument('--list-models', action='store_true')             # print the provider/model table and exit
    ap.add_argument('--output',      default='diff_output.html')      # output HTML filename
    args = ap.parse_args()

    if args.list_models:
        print(models_table())
        return

    # required in .env: whichever API key matches your provider (e.g. OPENAI_API_KEY if using openai)
    # optional in .env: LLM_PROVIDER (defaults to openai), OPENAI_MODEL, GEMINI_MODEL, CLAUDE_MODEL
    provider = args.provider or os.getenv('LLM_PROVIDER', 'openai')   # .env: LLM_PROVIDER
    if provider not in PROVIDERS:                                     # only reachable via a bad LLM_PROVIDER in .env
        ap.error(f"invalid provider {provider!r} (set via LLM_PROVIDER in .env).\n"
                 f"  valid providers: {', '.join(sorted(PROVIDERS))}")
    model, err = resolve_model(provider, args.model)
    if err:
        ap.error(err)                                                 # exits 2 with usage + the valid model list

    if args.auth_tok:
        token = args.auth_tok
    elif args.auth_file:
        tok_json = json.loads(Path(args.auth_file).read_text())
        token = tok_json['token']
    else:
        token = os.getenv(PROVIDERS[provider]['env_key'], '')  # .env: OPENAI_API_KEY / GEMINI_API_KEY / CLAUDE_API_KEY
    if not token:
        ap.error(f"no API key for provider {provider!r} — pass --auth-tok/--auth-file "
                 f"or set {PROVIDERS[provider]['env_key']} in .env")

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
    audit = out.with_name(out.stem + '_prompt.txt')   # sits beside --output, not in the cwd
    audit.write_text(f'PROMPT\n{"="*40}\n{prompt}\n\nPROVIDER: {provider}\nMODEL: {model}\n')
    print(f'Done — {out} + {audit}')

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
