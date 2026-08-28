"""Unit tests for smart_diff.

Network is never touched: provider tests monkeypatch requests.post and
capture the outgoing request for assertions. Environment variables are
controlled per-test via monkeypatch so a developer's shell or .env cannot
influence results.
"""

import hashlib
import json

import pytest
import requests

import smart_diff


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._body


@pytest.fixture
def capture_post(monkeypatch):
    """Replace requests.post; returns a dict recording the outgoing call.

    Set captured['response'] before invoking code to control what comes back.
    """
    captured = {"response": FakeResponse({})}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        return captured["response"]

    monkeypatch.setattr(smart_diff.requests, "post", fake_post)
    return captured


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every env var the providers read, so tests start from defaults."""
    for var in ("BEDROCK_REGION", "BEDROCK_ENDPOINT", "LLM_BASE_URL",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


BEDROCK_BODY = {"output": {"message": {"content": [{"text": "hello "}, {"text": "world"}]}}}
OPENAI_BODY = {"choices": [{"message": {"content": "hello world"}}]}


# ── strip_json_fences ────────────────────────────────────────────────────────

class TestStripJsonFences:
    def test_plain_text_passes_through(self):
        assert smart_diff.strip_json_fences('{"a": 1}') == '{"a": 1}'

    def test_strips_json_fence(self):
        assert smart_diff.strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_strips_bare_fence(self):
        assert smart_diff.strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_fence_tag_is_case_insensitive(self):
        assert smart_diff.strip_json_fences('```JSON\n{"a": 1}\n```') == '{"a": 1}'

    def test_surrounding_whitespace_stripped(self):
        assert smart_diff.strip_json_fences('  \n{"a": 1}\n  ') == '{"a": 1}'

    def test_backticks_inside_text_are_kept(self):
        text = 'prefix ```json\n{"a": 1}\n``` suffix'
        assert smart_diff.strip_json_fences(text) == text


# ── parse_diff ───────────────────────────────────────────────────────────────

VALID_DIFF = {
    "matches": ["both use MIL-STD-1553B"],
    "conflicts": [{"item": "rate", "value1": "100Hz", "value2": "50Hz"}],
    "missing": [{"item": "encryption", "missing_from": "doc2", "detail": ""}],
    "recommendation": "resolve the rate mismatch",
}


class TestParseDiff:
    def test_valid_json(self):
        assert smart_diff.parse_diff(json.dumps(VALID_DIFF)) == VALID_DIFF

    def test_valid_json_inside_fences(self):
        raw = f"```json\n{json.dumps(VALID_DIFF)}\n```"
        assert smart_diff.parse_diff(raw) == VALID_DIFF

    @pytest.mark.parametrize("missing_key", sorted(smart_diff.REQUIRED_KEYS))
    def test_missing_required_key_raises(self, missing_key):
        incomplete = {k: v for k, v in VALID_DIFF.items() if k != missing_key}
        with pytest.raises(ValueError, match=missing_key):
            smart_diff.parse_diff(json.dumps(incomplete))

    def test_non_json_raises_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            smart_diff.parse_diff("INVALID JSON DATA")


# ── read_file ────────────────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_text(self, tmp_path):
        p = tmp_path / "doc.txt"
        p.write_text("Interface voltage: 28V\n")
        assert smart_diff.read_file(p) == "Interface voltage: 28V\n"

    def test_undecodable_bytes_are_replaced_not_fatal(self, tmp_path):
        p = tmp_path / "doc.bin"
        p.write_bytes(b"ok \xff\xfe bytes")
        out = smart_diff.read_file(p)
        assert "ok" in out and "bytes" in out  # no UnicodeDecodeError

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            smart_diff.read_file(tmp_path / "nope.txt")


# ── sigv4_headers ────────────────────────────────────────────────────────────

class TestSigv4Headers:
    URL = "https://bedrock-runtime.us-gov-west-1.amazonaws.com/model/m1/converse"

    def sign(self, **kwargs):
        args = dict(method="POST", url=self.URL, region="us-gov-west-1",
                    service="bedrock", payload='{"x":1}',
                    access_key="AKIAEXAMPLE", secret_key="secretexample")
        args.update(kwargs)
        return smart_diff.sigv4_headers(**args)

    def test_content_sha256_matches_payload(self):
        headers = self.sign(payload='{"x":1}')
        expected = hashlib.sha256(b'{"x":1}').hexdigest()
        assert headers["X-Amz-Content-Sha256"] == expected

    def test_authorization_structure(self):
        headers = self.sign()
        auth = headers["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
        assert "/us-gov-west-1/bedrock/aws4_request" in auth
        assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
        signature = auth.rsplit("Signature=", 1)[1]
        assert len(signature) == 64 and int(signature, 16) is not None

    def test_session_token_signed_and_included(self):
        headers = self.sign(session_token="tok123")
        assert headers["X-Amz-Security-Token"] == "tok123"
        assert "x-amz-security-token" in headers["Authorization"]

    def test_no_session_token_header_by_default(self):
        assert "X-Amz-Security-Token" not in self.sign()

    def test_amz_date_format(self):
        amz_date = self.sign()["X-Amz-Date"]
        assert len(amz_date) == 16 and amz_date.endswith("Z") and amz_date[8] == "T"


# ── call_bedrock ─────────────────────────────────────────────────────────────

class TestCallBedrock:
    def test_bearer_token_request(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        result = smart_diff.call_bedrock("tok", "model-1", "sys", "msg")

        assert result == "hello world"
        assert capture_post["url"] == (
            "https://bedrock-runtime.us-gov-west-1.amazonaws.com/model/model-1/converse"
        )
        assert capture_post["headers"]["Authorization"] == "Bearer tok"
        assert capture_post["timeout"] == smart_diff.REQUEST_TIMEOUT_S
        body = json.loads(capture_post["data"])
        assert body["system"] == [{"text": "sys"}]
        assert body["messages"] == [{"role": "user", "content": [{"text": "msg"}]}]
        assert body["inferenceConfig"] == {"temperature": 0, "maxTokens": 8192}

    def test_model_id_is_url_quoted(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_bedrock("tok", "us.anthropic.claude/v1:0", "sys", "msg")
        assert "/model/us.anthropic.claude%2Fv1%3A0/converse" in capture_post["url"]

    def test_endpoint_override_and_trailing_slash(self, capture_post, clean_env):
        clean_env.setenv("BEDROCK_ENDPOINT", "https://vpce-123.example.amazonaws.com/")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_bedrock("tok", "m1", "sys", "msg")
        assert capture_post["url"] == "https://vpce-123.example.amazonaws.com/model/m1/converse"

    def test_region_env_changes_default_endpoint(self, capture_post, clean_env):
        clean_env.setenv("BEDROCK_REGION", "us-east-1")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_bedrock("tok", "m1", "sys", "msg")
        assert capture_post["url"].startswith("https://bedrock-runtime.us-east-1.amazonaws.com/")

    def test_sigv4_used_when_no_token(self, capture_post, clean_env):
        clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_bedrock("", "m1", "sys", "msg")
        auth = capture_post["headers"]["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")

    def test_no_auth_raises_before_any_request(self, capture_post, clean_env):
        with pytest.raises(RuntimeError, match="auth missing"):
            smart_diff.call_bedrock("", "m1", "sys", "msg")
        assert "url" not in capture_post  # nothing was sent

    def test_http_error_propagates(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse({}, status=403)
        with pytest.raises(requests.exceptions.HTTPError):
            smart_diff.call_bedrock("tok", "m1", "sys", "msg")


# ── call_openai_compat ───────────────────────────────────────────────────────

class TestCallOpenaiCompat:
    def test_request_shape_and_response(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(OPENAI_BODY)
        result = smart_diff.call_openai_compat("tok", "gpt-x", "sys", "msg")

        assert result == "hello world"
        assert capture_post["url"] == "https://api.openai.com/v1/chat/completions"
        assert capture_post["headers"]["Authorization"] == "Bearer tok"
        assert capture_post["timeout"] == smart_diff.REQUEST_TIMEOUT_S
        body = json.loads(capture_post["data"])
        assert body["model"] == "gpt-x"
        assert body["temperature"] == 0
        assert body["messages"] == [{"role": "system", "content": "sys"},
                                    {"role": "user", "content": "msg"}]

    def test_base_url_override_and_trailing_slash(self, capture_post, clean_env):
        clean_env.setenv("LLM_BASE_URL", "https://llm.internal.example/v1/")
        capture_post["response"] = FakeResponse(OPENAI_BODY)
        smart_diff.call_openai_compat("tok", "m", "sys", "msg")
        assert capture_post["url"] == "https://llm.internal.example/v1/chat/completions"

    def test_http_error_propagates(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse({}, status=429)
        with pytest.raises(requests.exceptions.HTTPError):
            smart_diff.call_openai_compat("tok", "m", "sys", "msg")


# ── call_llm dispatch ────────────────────────────────────────────────────────

class TestCallLlm:
    def test_dispatches_to_bedrock(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        assert smart_diff.call_llm("bedrock", "tok", "m1", "sys", "msg") == "hello world"
        assert "/converse" in capture_post["url"]

    def test_dispatches_to_openai_compat(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(OPENAI_BODY)
        assert smart_diff.call_llm("openai_compat", "tok", "m1", "sys", "msg") == "hello world"
        assert capture_post["url"].endswith("/chat/completions")

    def test_provider_registry_matches_dispatchable_set(self):
        assert set(smart_diff.PROVIDERS) == {"bedrock", "openai_compat"}
