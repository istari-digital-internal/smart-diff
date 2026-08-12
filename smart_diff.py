"""Smart Diff: AI comparison of two extracted document artifacts.

Reads two plain-text inputs (products of platform extraction jobs), sends them
with a fixed system prompt to an approved LLM endpoint over HTTPS, and writes
an HTML comparison report plus a plain-text audit file.

Runtime dependencies: python-dotenv, requests, and the standard library only.
No LLM vendor SDKs and no AWS SDK. Providers:

  bedrock        AWS Bedrock Converse API (GovCloud/FIPS endpoint).
                 Auth: bearer API key, or AWS SigV4 signed with stdlib hmac.
  openai_compat  Any OpenAI-compatible chat completions endpoint.
                 Auth: bearer API key.
"""

import argparse
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path
from string import Template

from dotenv import load_dotenv
import requests

REQUIRED_KEYS = ("matches", "conflicts", "missing", "recommendation")
REQUEST_TIMEOUT_S = 300


def read_file(p):
    # Inputs are extraction products (plain text, HTML, or JSON exports).
    return Path(p).read_text(errors="replace")


def strip_json_fences(text):
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text, re.IGNORECASE)
    return fenced.group(1).strip() if fenced else text


def parse_diff(raw):
    diff = json.loads(strip_json_fences(raw))
    for key in REQUIRED_KEYS:
        if key not in diff:
            raise ValueError(f"LLM response missing required key: {key!r}")
    return diff


# ── SigV4 signing (stdlib only) ──────────────────────────────────────────────

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method, url, region, service, payload, access_key, secret_key, session_token=None):
    # AWS Signature Version 4 for a single POST request.
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    canonical_uri = urllib.parse.quote(parsed.path or "/")
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))

    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_names, payload_hash]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope,
         hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
    )
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    out = {
        "X-Amz-Date": amz_date,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        ),
    }
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out


# ── Providers ────────────────────────────────────────────────────────────────

def call_bedrock(token, model, system, msg):
    # Converse API: one request shape regardless of the underlying model.
    region = os.getenv("BEDROCK_REGION", "us-gov-west-1")
    endpoint = os.getenv(
        "BEDROCK_ENDPOINT", f"https://bedrock-runtime.{region}.amazonaws.com"
    ).rstrip("/")
    url = f"{endpoint}/model/{urllib.parse.quote(model, safe='')}/converse"
    payload = json.dumps({
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": msg}]}],
        "inferenceConfig": {"temperature": 0, "maxTokens": 8192},
    })

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif access_key and secret_key:
        headers.update(sigv4_headers(
            "POST", url, region, "bedrock", payload, access_key, secret_key,
            os.getenv("AWS_SESSION_TOKEN") or None,
        ))
    else:
        raise RuntimeError("Bedrock auth missing: provide --auth-tok or AWS credentials")

    resp = requests.post(url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    blocks = resp.json()["output"]["message"]["content"]
    return "".join(b.get("text", "") for b in blocks)


def call_openai_compat(token, model, system, msg):
    # Any endpoint speaking the chat completions contract.
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    payload = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": msg},
        ],
    })
    resp = requests.post(
        f"{base}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


PROVIDERS = {
    "bedrock": (call_bedrock, "BEDROCK_MODEL_ID"),
    "openai_compat": (call_openai_compat, "LLM_MODEL"),
}


def call_llm(provider, token, model, system, msg):
    fn, _ = PROVIDERS[provider]
    return fn(token, model, system, msg)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)                    # user focus text
    ap.add_argument("--diff-file1", required=True)                # extracted artifact A
    ap.add_argument("--diff-file2", required=True)                # extracted artifact B
    ap.add_argument("--auth-tok", default=None)                   # LLM API key/token
    ap.add_argument("--provider", default=None)                   # bedrock | openai_compat
    ap.add_argument("--file1-uuid", default=None)
    ap.add_argument("--file1-rev", default=None)
    ap.add_argument("--file2-uuid", default=None)
    ap.add_argument("--file2-rev", default=None)
    ap.add_argument("--output", default="diff_output.html")
    args = ap.parse_args()

    provider = args.provider or os.getenv("LLM_PROVIDER", "bedrock")
    if provider not in PROVIDERS:
        raise SystemExit(f"Unknown provider {provider!r}; expected one of {sorted(PROVIDERS)}")
    _, model_env = PROVIDERS[provider]
    model = os.getenv(model_env, "")
    if not model:
        raise SystemExit(f"Set {model_env} in the environment or .env")
    token = args.auth_tok or os.getenv("LLM_AUTH_TOKEN", "")

    system = (Path(__file__).parent / "system_prompt.txt").read_text().strip()
    prompt = args.prompt.strip()
    filename1, filename2 = Path(args.diff_file1).name, Path(args.diff_file2).name
    uuid1, rev1 = (args.file1_uuid or ""), (args.file1_rev or "")
    uuid2, rev2 = (args.file2_uuid or ""), (args.file2_rev or "")
    f1, f2 = read_file(args.diff_file1), read_file(args.diff_file2)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    msg = f"""Return ONLY valid JSON in this exact format:
{{"matches":["..."],"conflicts":[{{"item":"","value1":"","value2":""}}],"missing":[{{"item":"","missing_from":"","detail":""}}],"recommendation":"..."}}

User focus: {prompt}

--- Document 1 | {filename1} | UUID: {uuid1} | Revision: {rev1} ---
{f1}

--- Document 2 | {filename2} | UUID: {uuid2} | Revision: {rev2} ---
{f2}"""

    raw = call_llm(provider, token, model, system, msg)
    try:
        diff = parse_diff(raw)
    except (ValueError, json.JSONDecodeError):
        # One retry; a second malformed response fails the job cleanly.
        raw = call_llm(provider, token, model, system, msg)
        diff = parse_diff(raw)

    out = Path(args.output)
    out.write_text(Template(
        (Path(__file__).parent / "html" / "report_template.html")
        .read_text()).substitute(
        filename1=filename1, filename2=filename2,
        uuid1=uuid1, rev1=rev1, uuid2=uuid2, rev2=rev2,
        provider=provider, model=model, timestamp=timestamp,
        matches_html=''.join(f'<li>{m}</li>' for m in diff['matches']),
        conflicts_html=''.join(
            f'<tr style="border-bottom:1px solid #ddd">'
            f'<td style="padding:8px">{c["item"]}</td>'
            f'<td style="padding:8px">{c["value1"]}</td>'
            f'<td style="padding:8px">{c["value2"]}</td></tr>'
            for c in diff['conflicts']),
        missing_html=''.join(
            f'<li><b>{m["missing_from"]}</b> did not specify {m["item"]}. {m.get("detail", "")}</li>'
            for m in diff['missing']),
        recommendation=diff['recommendation'],
    ))
    Path(out.stem + "_prompt.txt").write_text(
        f'PROMPT\n{"=" * 40}\n{prompt}\n\nPROVIDER: {provider}\nMODEL: {model}\n'
    )
    print(f"Done — {out} + {out.stem}_prompt.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
