"""Unit tests for smart_diff.

No network and no real document parsing:
- Provider/request tests replace requests.post with a fake that records the
  outgoing call, so assertions run against exactly what would be sent.
- read_file tests inject fake pdfplumber/openpyxl/docx modules into
  sys.modules (the script imports them lazily inside the function).
- Environment variables are controlled per-test via monkeypatch, and main()
  tests neutralize load_dotenv so the repo's real .env can't leak in.
"""

import hashlib
import json
import sys
import types

import pytest
import requests

import smart_diff


# ── Fakes and fixtures ───────────────────────────────────────────────────────

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
    """Remove every env var the script reads, so tests start from defaults."""
    for var in ("LLM_PROVIDER",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "CLAUDE_API_KEY", "BEDROCK_API_KEY",
                "OPENAI_MODEL", "GEMINI_MODEL", "CLAUDE_MODEL", "BEDROCK_MODEL",
                "BEDROCK_REGION", "BEDROCK_ENDPOINT",
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


OPENAI_BODY = {"choices": [{"message": {"content": "openai says hi"}}]}
GEMINI_BODY = {"candidates": [{"content": {"parts": [{"text": "gem"}, {"text": "ini"}]}}]}
CLAUDE_BODY = {"content": [{"type": "thinking", "thinking": "..."},
                           {"type": "text", "text": "claude says hi"}]}
BEDROCK_BODY = {"output": {"message": {"content": [{"text": "bed"}, {"text": "rock"}]}}}


# ── resolve_model / default_model / models_table ─────────────────────────────

class TestResolveModel:
    def test_default_when_nothing_set(self, clean_env):
        model, err = smart_diff.resolve_model("openai", None)
        assert err is None
        assert model == smart_diff.PROVIDERS["openai"]["models"][0]

    def test_valid_cli_model(self, clean_env):
        model, err = smart_diff.resolve_model("openai", "gpt-4o-mini")
        assert (model, err) == ("gpt-4o-mini", None)

    def test_valid_env_model(self, clean_env):
        clean_env.setenv("CLAUDE_MODEL", "sonnet-5")
        model, err = smart_diff.resolve_model("claude", None)
        assert (model, err) == ("sonnet-5", None)

    def test_cli_beats_env(self, clean_env):
        clean_env.setenv("OPENAI_MODEL", "gpt-4o-mini")
        model, err = smart_diff.resolve_model("openai", "gpt-4.1")
        assert (model, err) == ("gpt-4.1", None)

    def test_invalid_cli_model_names_flag_as_source(self, clean_env):
        model, err = smart_diff.resolve_model("openai", "gpt-9000")
        assert model is None
        assert "gpt-9000" in err and "--model" in err

    def test_invalid_env_model_names_env_var_as_source(self, clean_env):
        clean_env.setenv("GEMINI_MODEL", "bard-1")
        model, err = smart_diff.resolve_model("gemini", None)
        assert model is None
        assert "GEMINI_MODEL" in err

    def test_error_lists_valid_models(self, clean_env):
        _, err = smart_diff.resolve_model("bedrock", "nope")
        for valid in smart_diff.PROVIDERS["bedrock"]["models"]:
            assert valid in err

    @pytest.mark.parametrize("provider", sorted(smart_diff.PROVIDERS))
    def test_every_provider_has_a_default(self, clean_env, provider):
        model, err = smart_diff.resolve_model(provider, None)
        assert err is None and model


class TestModelsTable:
    def test_all_providers_and_default_marker(self):
        table = smart_diff.models_table()
        for provider, cfg in smart_diff.PROVIDERS.items():
            assert provider in table
            assert f"{cfg['models'][0]}  (default)" in table


# ── read_file ────────────────────────────────────────────────────────────────

class TestReadFile:
    def test_pdf_uses_pdfplumber_and_joins_pages(self, monkeypatch, tmp_path):
        class FakePage:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class FakePdf:
            pages = [FakePage("page one"), FakePage(None), FakePage("page three")]

        fake = types.SimpleNamespace(open=lambda p: FakePdf())
        monkeypatch.setitem(sys.modules, "pdfplumber", fake)
        # extract_text() -> None becomes '' (blank line), not a crash
        assert smart_diff.read_file(tmp_path / "x.pdf") == "page one\n\npage three"

    def test_xlsx_uses_openpyxl_skips_none_cells(self, monkeypatch, tmp_path):
        class FakeSheet:
            def iter_rows(self, values_only):
                assert values_only is True
                yield ("volts", 28, None)
                yield (None, "amps", 3)

        class FakeWb:
            worksheets = [FakeSheet()]

        def load_workbook(p, data_only):
            assert data_only is True   # values, not formulas
            return FakeWb()

        monkeypatch.setitem(sys.modules, "openpyxl",
                            types.SimpleNamespace(load_workbook=load_workbook))
        assert smart_diff.read_file(tmp_path / "x.xlsx") == "volts  |  28\namps  |  3"

    def test_docx_uses_document_and_drops_blank_paragraphs(self, monkeypatch, tmp_path):
        class FakePara:
            def __init__(self, text):
                self.text = text

        class FakeDoc:
            paragraphs = [FakePara("alpha"), FakePara("   "), FakePara("beta")]

        monkeypatch.setitem(sys.modules, "docx",
                            types.SimpleNamespace(Document=lambda p: FakeDoc()))
        assert smart_diff.read_file(tmp_path / "x.docx") == "alpha\nbeta"

    def test_extension_check_is_case_insensitive(self, monkeypatch, tmp_path):
        called = {}

        class FakePdf:
            pages = []

        def fake_open(p):
            called["pdf"] = True
            return FakePdf()

        monkeypatch.setitem(sys.modules, "pdfplumber", types.SimpleNamespace(open=fake_open))
        smart_diff.read_file(tmp_path / "UPPER.PDF")
        assert called.get("pdf")

    def test_plain_text_fallback(self, tmp_path):
        p = tmp_path / "doc.txt"
        p.write_text("voltage: 28V\n")
        assert smart_diff.read_file(p) == "voltage: 28V\n"

    def test_undecodable_bytes_replaced_not_fatal(self, tmp_path):
        p = tmp_path / "doc.csv"
        p.write_bytes(b"ok \xff\xfe bytes")
        out = smart_diff.read_file(p)
        assert "ok" in out and "bytes" in out

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            smart_diff.read_file(tmp_path / "nope.txt")


# ── _sigv4_headers ───────────────────────────────────────────────────────────

class TestSigv4Headers:
    URL = "https://bedrock-runtime.us-gov-west-1.amazonaws.com/model/m1/converse"

    def sign(self, **kwargs):
        args = dict(url=self.URL, region="us-gov-west-1", payload='{"x":1}',
                    access_key="AKIAEXAMPLE", secret_key="secretexample")
        args.update(kwargs)
        return smart_diff._sigv4_headers(**args)

    def test_content_sha256_matches_payload(self):
        headers = self.sign()
        assert headers["X-Amz-Content-Sha256"] == hashlib.sha256(b'{"x":1}').hexdigest()

    def test_authorization_structure(self):
        auth = self.sign()["Authorization"]
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


# ── _chat_completions ────────────────────────────────────────────────────────

class TestChatCompletions:
    def test_request_shape_and_response(self, capture_post):
        capture_post["response"] = FakeResponse(OPENAI_BODY)
        result = smart_diff._chat_completions(
            "https://api.openai.com/v1/", "tok", "gpt-4o", "sys", "msg")

        assert result == "openai says hi"
        # trailing slash on base is normalized away
        assert capture_post["url"] == "https://api.openai.com/v1/chat/completions"
        assert capture_post["headers"]["Authorization"] == "Bearer tok"
        assert capture_post["timeout"] == smart_diff.REQUEST_TIMEOUT_S
        body = json.loads(capture_post["data"])
        assert body["model"] == "gpt-4o"
        assert body["messages"] == [{"role": "system", "content": "sys"},
                                    {"role": "user", "content": "msg"}]

    def test_http_error_propagates(self, capture_post):
        capture_post["response"] = FakeResponse({}, status=429)
        with pytest.raises(requests.exceptions.HTTPError):
            smart_diff._chat_completions("https://x", "tok", "m", "sys", "msg")


# ── call_llm per provider ────────────────────────────────────────────────────

class TestCallLlmOpenai:
    def test_routes_to_chat_completions(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(OPENAI_BODY)
        assert smart_diff.call_llm("openai", "tok", "gpt-4o", "sys", "msg") == "openai says hi"
        assert capture_post["url"] == "https://api.openai.com/v1/chat/completions"


class TestCallLlmGemini:
    def test_url_gets_gemini_prefix_and_quoting(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(GEMINI_BODY)
        result = smart_diff.call_llm("gemini", "tok", "2.5-flash", "sys", "msg")

        assert result == "gemini"
        assert capture_post["url"] == ("https://generativelanguage.googleapis.com"
                                       "/v1beta/models/gemini-2.5-flash:generateContent")
        assert capture_post["headers"]["x-goog-api-key"] == "tok"
        body = json.loads(capture_post["data"])
        assert body["system_instruction"] == {"parts": [{"text": "sys"}]}
        assert body["contents"] == [{"role": "user", "parts": [{"text": "msg"}]}]

    def test_parts_without_text_are_skipped(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(
            {"candidates": [{"content": {"parts": [{"text": "a"}, {"inlineData": {}}]}}]})
        assert smart_diff.call_llm("gemini", "tok", "2.5-pro", "sys", "msg") == "a"


class TestCallLlmClaude:
    def test_request_shape_and_response(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(CLAUDE_BODY)
        result = smart_diff.call_llm("claude", "tok", "opus-5", "sys", "msg")

        # thinking blocks (no 'text' key) are skipped; only text blocks join
        assert result == "claude says hi"
        assert capture_post["url"] == "https://api.anthropic.com/v1/messages"
        assert capture_post["headers"]["x-api-key"] == "tok"
        assert capture_post["headers"]["anthropic-version"] == "2023-06-01"
        body = json.loads(capture_post["data"])
        assert body["max_tokens"] == 16000
        assert body["system"] == "sys"
        assert body["messages"] == [{"role": "user", "content": "msg"}]


class TestCallLlmBedrock:
    def test_bearer_token_request(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        result = smart_diff.call_llm("bedrock", "tok", "opus-5", "sys", "msg")

        assert result == "bedrock"
        # default region is GovCloud
        assert capture_post["url"] == (
            "https://bedrock-runtime.us-gov-west-1.amazonaws.com/model/opus-5/converse")
        assert capture_post["headers"]["Authorization"] == "Bearer tok"
        assert capture_post["timeout"] == smart_diff.REQUEST_TIMEOUT_S
        body = json.loads(capture_post["data"])
        assert body["system"] == [{"text": "sys"}]
        assert body["messages"] == [{"role": "user", "content": [{"text": "msg"}]}]
        assert body["inferenceConfig"]["maxTokens"] == 16000

    def test_model_id_is_url_quoted(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_llm("bedrock", "tok", "us.anthropic.claude/v1:0", "sys", "msg")
        assert "/model/us.anthropic.claude%2Fv1%3A0/converse" in capture_post["url"]

    def test_region_env_changes_default_endpoint(self, capture_post, clean_env):
        clean_env.setenv("BEDROCK_REGION", "us-east-1")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_llm("bedrock", "tok", "m", "sys", "msg")
        assert capture_post["url"].startswith("https://bedrock-runtime.us-east-1.amazonaws.com/")

    def test_endpoint_override_beats_region_and_strips_slash(self, capture_post, clean_env):
        clean_env.setenv("BEDROCK_REGION", "us-east-1")
        clean_env.setenv("BEDROCK_ENDPOINT", "https://vpce-123.example.amazonaws.com/")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_llm("bedrock", "tok", "m", "sys", "msg")
        assert capture_post["url"] == "https://vpce-123.example.amazonaws.com/model/m/converse"

    def test_sigv4_used_when_no_token(self, capture_post, clean_env):
        clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_llm("bedrock", "", "m", "sys", "msg")
        auth = capture_post["headers"]["Authorization"]
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")

    def test_session_token_flows_through(self, capture_post, clean_env):
        clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
        clean_env.setenv("AWS_SESSION_TOKEN", "sess")
        capture_post["response"] = FakeResponse(BEDROCK_BODY)
        smart_diff.call_llm("bedrock", "", "m", "sys", "msg")
        assert capture_post["headers"]["X-Amz-Security-Token"] == "sess"

    def test_no_auth_raises_before_any_request(self, capture_post, clean_env):
        with pytest.raises(RuntimeError, match="auth missing"):
            smart_diff.call_llm("bedrock", "", "m", "sys", "msg")
        assert "url" not in capture_post  # nothing was sent

    def test_http_error_propagates(self, capture_post, clean_env):
        capture_post["response"] = FakeResponse({}, status=403)
        with pytest.raises(requests.exceptions.HTTPError):
            smart_diff.call_llm("bedrock", "tok", "m", "sys", "msg")


class TestCallLlmDispatch:
    def test_unknown_provider_exits(self, clean_env):
        with pytest.raises(SystemExit):
            smart_diff.call_llm("carrier_pigeon", "tok", "m", "sys", "msg")


# ── main() ───────────────────────────────────────────────────────────────────

VALID_DIFF = {"matches": ["both use 1553B"],
              "conflicts": [{"item": "rate", "value1": "100Hz", "value2": "50Hz"}],
              "missing": [{"item": "encryption", "missing_from": "doc2", "detail": "tbd"}],
              "recommendation": "resolve the rate mismatch"}


@pytest.fixture
def cli(monkeypatch, clean_env, tmp_path):
    """Prepare an isolated main() invocation; returns a runner function.

    Neutralizes load_dotenv so the repo's real .env cannot leak into tests.
    """
    monkeypatch.setattr(smart_diff, "load_dotenv", lambda: None)
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("doc one")
    f2.write_text("doc two")

    def run(*extra_args):
        argv = ["smart_diff.py",
                "--prompt", "compare",
                "--diff-file1", str(f1), "--diff-file2", str(f2),
                "--output", str(tmp_path / "out.html"), *extra_args]
        monkeypatch.setattr(sys, "argv", argv)
        return smart_diff.main()

    run.tmp_path = tmp_path
    return run


class TestMain:
    def test_success_writes_report_and_audit(self, cli, capture_post):
        raw = json.dumps(VALID_DIFF)
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": raw}}]})
        cli("--provider", "openai", "--auth-tok", "tok")

        html = (cli.tmp_path / "out.html").read_text()
        assert "100Hz" in html and "resolve the rate mismatch" in html
        audit = (cli.tmp_path / "out_prompt.txt").read_text()
        assert "compare" in audit and "openai" in audit

    def test_fenced_llm_response_is_parsed(self, cli, capture_post):
        raw = f"```json\n{json.dumps(VALID_DIFF)}\n```"
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": raw}}]})
        cli("--provider", "openai", "--auth-tok", "tok")
        assert (cli.tmp_path / "out.html").exists()

    def test_document_contents_reach_the_llm(self, cli, capture_post):
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(VALID_DIFF)}}]})
        cli("--provider", "openai", "--auth-tok", "tok")
        sent = json.loads(capture_post["data"])["messages"][1]["content"]
        assert "doc one" in sent and "doc two" in sent and "compare" in sent

    def test_env_token_used_when_no_flag(self, cli, capture_post, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "env-tok")
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(VALID_DIFF)}}]})
        cli("--provider", "openai")
        assert capture_post["headers"]["Authorization"] == "Bearer env-tok"

    def test_auth_file_used_when_no_flag(self, cli, capture_post):
        tok_file = cli.tmp_path / "secret.json"
        tok_file.write_text(json.dumps({"token": "file-tok"}))
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(VALID_DIFF)}}]})
        cli("--provider", "openai", "--auth-file", str(tok_file))
        assert capture_post["headers"]["Authorization"] == "Bearer file-tok"

    def test_auth_tok_beats_auth_file(self, cli, capture_post):
        tok_file = cli.tmp_path / "secret.json"
        tok_file.write_text(json.dumps({"token": "file-tok"}))
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(VALID_DIFF)}}]})
        cli("--provider", "openai", "--auth-file", str(tok_file), "--auth-tok", "flag-tok")
        assert capture_post["headers"]["Authorization"] == "Bearer flag-tok"

    def test_missing_token_exits_with_usage_error(self, cli):
        with pytest.raises(SystemExit) as exc:
            cli("--provider", "openai")
        assert exc.value.code == 2   # argparse ap.error exit code

    def test_invalid_model_exits_with_usage_error(self, cli):
        with pytest.raises(SystemExit) as exc:
            cli("--provider", "openai", "--auth-tok", "tok", "--model", "gpt-9000")
        assert exc.value.code == 2

    def test_invalid_provider_flag_rejected_by_argparse(self, cli):
        with pytest.raises(SystemExit) as exc:
            cli("--provider", "carrier_pigeon")
        assert exc.value.code == 2

    def test_bad_env_provider_exits_with_usage_error(self, cli, clean_env):
        clean_env.setenv("LLM_PROVIDER", "carrier_pigeon")
        with pytest.raises(SystemExit) as exc:
            cli("--auth-tok", "tok")
        assert exc.value.code == 2

    def test_llm_markup_is_escaped_in_report(self, cli, capture_post):
        hostile = dict(VALID_DIFF,
                       matches=["<script>alert(1)</script>"],
                       recommendation='see <img src=x onerror="steal()">')
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(hostile)}}]})
        cli("--provider", "openai", "--auth-tok", "tok")

        report = (cli.tmp_path / "out.html").read_text()
        # the raw executable forms must be absent…
        assert "<script>alert(1)</script>" not in report
        assert 'onerror="steal()"' not in report
        # …and the entity-encoded (inert, human-readable) forms present
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
        assert "onerror=&quot;steal()&quot;" in report

    def test_legitimate_angle_brackets_survive_as_text(self, cli, capture_post):
        engineering = dict(VALID_DIFF,
                           conflicts=[{"item": "voltage", "value1": "< 5V",
                                       "value2": "5V & 28V"}])
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(engineering)}}]})
        cli("--provider", "openai", "--auth-tok", "tok")

        report = (cli.tmp_path / "out.html").read_text()
        assert "&lt; 5V" in report and "5V &amp; 28V" in report

    def test_non_string_llm_values_do_not_crash_report(self, cli, capture_post):
        odd = dict(VALID_DIFF, matches=[42, None])
        capture_post["response"] = FakeResponse(
            {"choices": [{"message": {"content": json.dumps(odd)}}]})
        cli("--provider", "openai", "--auth-tok", "tok")
        report = (cli.tmp_path / "out.html").read_text()
        assert "<li>42</li>" in report

    def test_list_models_prints_table_and_exits_early(self, cli, capsys, capture_post):
        cli("--list-models")   # no token/model needed; returns before provider logic
        out = capsys.readouterr().out
        assert "bedrock" in out and "(default)" in out
        assert "url" not in capture_post  # no request was made
