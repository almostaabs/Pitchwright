"""Every Anthropic and Gmail call is mocked. Nothing here touches the network.

Covered: the validate retry loop, the send cap, the dedup check, the
NEEDS_REVIEW transition on repeated validation failure and on a send error,
and the permanent exclusion of a REPLIED prospect from follow-ups.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import followup
from sponsor_agent import config, gmail_client, graph, llm, sheet

FACTS = {"sender_name": "Sample Sender", "reply_to": "notreal@example.com"}
GOOD = llm.Draft(
    subject="Sponsoring Acme Robotics at Placeholder Tech Fest",
    body=(
        "Hi Dana, Sample Sender here from the Example Student Tech Collective. "
        "Acme Robotics came up when we listed the companies our attendees ask about. "
        + "We are looking for a title sponsor for Placeholder Tech Fest. " * 6
        + "Would you be open to a short call? You can reach me at notreal@example.com. "
        "Sample Sender, Sponsorship Lead."
    ),
)
PASS = llm.Verdict(passed=True)
FAIL = llm.Verdict(passed=False, errors=["invented an attendance number"])


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated workbook and input file, with no network anywhere."""
    monkeypatch.setattr(config, "WORKBOOK", tmp_path / "outreach.xlsx")
    monkeypatch.setattr(config, "INPUT_FILE", tmp_path / "prospects_input.csv")
    monkeypatch.setattr(config, "SEND_CAP", 25)
    monkeypatch.setattr(config, "MAX_VALIDATION_RETRIES", 2)
    monkeypatch.setattr(config, "FOLLOWUP_DAYS", 5)
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(graph.research_mod, "research", lambda prospect: "no data found")
    sheet.ensure_workbook()
    return tmp_path


def write_input(tmp_path, count=1):
    lines = ["company,contact_name,contact_email,website,notes"]
    for i in range(count):
        lines.append(f"Fake Co {i},Pat Fictional,fake{i}@example.com,,synthetic test row")
    (tmp_path / "prospects_input.csv").write_text("\n".join(lines), encoding="utf-8")


def _sequence(verdicts):
    remaining = list(verdicts)

    def fake_validate(*args, **kwargs):
        return remaining.pop(0) if remaining else PASS

    return fake_validate


def mock_llm(monkeypatch, verdicts, draft=GOOD):
    """Returns a call-count dict. `verdicts` is consumed one per validate call."""
    calls = {"draft": 0, "validate": 0}
    verdict_source = _sequence(verdicts)

    def fake_draft(*args, **kwargs):
        calls["draft"] += 1
        return draft

    def fake_validate(*args, **kwargs):
        calls["validate"] += 1
        return verdict_source()

    monkeypatch.setattr(llm, "draft_email", fake_draft)
    monkeypatch.setattr(llm, "validate_draft", fake_validate)
    return calls


def mock_gmail(monkeypatch, fail_on=()):
    sends = []

    def fake_send(to, subject, body, thread_id=None):
        sends.append(to)
        if to in fail_on:
            raise RuntimeError("Gmail API error 550: mailbox unavailable")
        return {"id": f"m{len(sends)}", "threadId": f"t{len(sends)}"}

    monkeypatch.setattr(gmail_client, "send_email", fake_send)
    return sends


def rows_by_email(email):
    return [r for r in sheet.read_rows() if r["contact_email"] == email]


# --- the validate retry loop ------------------------------------------------


def test_validation_failure_retries_twice_then_needs_review(env, monkeypatch):
    write_input(env, 1)
    calls = mock_llm(monkeypatch, [FAIL, FAIL, FAIL])
    sends = mock_gmail(monkeypatch)

    final = graph.run(entry="ingest", facts=FACTS)

    assert calls["draft"] == 3, "one initial draft plus MAX_VALIDATION_RETRIES redrafts"
    assert calls["validate"] == 3
    assert sends == [], "a draft that never passed must never be sent"
    assert final.get("sent", 0) == 0

    row = rows_by_email("fake0@example.com")[0]
    assert row["status"] == sheet.NEEDS_REVIEW
    assert "invented an attendance number" in row["last_error"]
    assert "after 2 retries" in row["last_error"]


def test_retry_feedback_reaches_the_next_draft(env, monkeypatch):
    write_input(env, 1)
    seen_feedback = []

    def fake_draft(prospect, facts, notes, feedback=None, previous=None):
        seen_feedback.append(feedback)
        return GOOD

    monkeypatch.setattr(llm, "draft_email", fake_draft)
    monkeypatch.setattr(llm, "validate_draft", _sequence([FAIL, PASS]))
    mock_gmail(monkeypatch)

    graph.run(entry="ingest", facts=FACTS)

    assert seen_feedback[0] is None
    assert seen_feedback[1] == ["invented an attendance number"]


def test_draft_passing_on_the_last_retry_still_sends(env, monkeypatch):
    write_input(env, 1)
    calls = mock_llm(monkeypatch, [FAIL, FAIL, PASS])
    sends = mock_gmail(monkeypatch)

    final = graph.run(entry="ingest", facts=FACTS)

    assert calls["draft"] == 3
    assert sends == ["fake0@example.com"]
    assert final["sent"] == 1
    assert rows_by_email("fake0@example.com")[0]["status"] == sheet.SENT


# --- guardrails: send cap ---------------------------------------------------


def test_send_cap_stops_the_run_and_leaves_the_rest_for_next_time(env, monkeypatch):
    monkeypatch.setattr(config, "SEND_CAP", 2)
    write_input(env, 5)
    mock_llm(monkeypatch, [])
    sends = mock_gmail(monkeypatch)

    final = graph.run(entry="ingest", facts=FACTS)

    assert final["sent"] == 2
    assert len(sends) == 2, "the cap is checked before drafting, not after sending"

    statuses = [r["status"] for r in sheet.read_rows()]
    assert statuses.count(sheet.SENT) == 2
    assert statuses.count(sheet.NEW) == 3, "uncapped rows stay NEW for the next run"
    assert any("cap of 2 sends reached" in entry["outcome"] for entry in sheet.log_rows())


# --- guardrails: dedup ------------------------------------------------------


def test_same_email_and_touch_is_never_sent_twice(env, monkeypatch):
    write_input(env, 1)
    mock_llm(monkeypatch, [])
    sends = mock_gmail(monkeypatch)

    graph.run(entry="ingest", facts=FACTS)
    assert sends == ["fake0@example.com"]

    # A duplicate row for the same contact at the same touch sneaks onto the sheet.
    sheet.append(
        {
            "company": "Fake Co 0 (duplicate)",
            "contact_email": "fake0@example.com",
            "status": sheet.NEW,
            "touch_number": 1,
        }
    )
    graph.run(entry="ingest", facts=FACTS)

    assert sends == ["fake0@example.com"], "dedup holds across separate runs"
    duplicate = rows_by_email("fake0@example.com")[1]
    assert duplicate["status"] == sheet.NEEDS_REVIEW
    assert "already delivered" in duplicate["last_error"]


def test_already_sent_matches_case_insensitively(env):
    sheet.append(
        {"contact_email": "Fake0@Example.com", "status": sheet.SENT, "touch_number": 1}
    )
    assert sheet.already_sent("fake0@example.com", 1)
    assert not sheet.already_sent("fake0@example.com", 2)
    assert not sheet.already_sent("someone.else@example.com", 1)


# --- exceptions: send failure and API outage --------------------------------


def test_send_error_parks_the_row_and_the_run_continues(env, monkeypatch):
    write_input(env, 2)
    mock_llm(monkeypatch, [])
    sends = mock_gmail(monkeypatch, fail_on={"fake0@example.com"})

    final = graph.run(entry="ingest", facts=FACTS)

    assert sends == ["fake0@example.com", "fake1@example.com"]
    assert final["sent"] == 1, "a failed send does not count against the cap"

    failed = rows_by_email("fake0@example.com")[0]
    assert failed["status"] == sheet.NEEDS_REVIEW
    assert "mailbox unavailable" in failed["last_error"]
    assert rows_by_email("fake1@example.com")[0]["status"] == sheet.SENT


def test_anthropic_outage_ends_as_needs_review_not_a_crash(env, monkeypatch):
    write_input(env, 1)

    def boom(*args, **kwargs):
        raise RuntimeError("anthropic: 529 overloaded")

    monkeypatch.setattr(llm, "draft_email", boom)
    monkeypatch.setattr(llm, "validate_draft", boom)
    sends = mock_gmail(monkeypatch)

    graph.run(entry="ingest", facts=FACTS)

    row = rows_by_email("fake0@example.com")[0]
    assert row["status"] == sheet.NEEDS_REVIEW
    assert "529 overloaded" in row["last_error"]
    assert sends == [], "a row with no usable draft is never sent"


# --- the audit trail --------------------------------------------------------


def test_every_node_writes_to_the_log_sheet(env, monkeypatch):
    write_input(env, 1)
    mock_llm(monkeypatch, [])
    mock_gmail(monkeypatch)

    graph.run(entry="ingest", facts=FACTS)

    nodes = {entry["node"] for entry in sheet.log_rows()}
    assert {"ingest", "research", "personalize", "validate", "send"} <= nodes


# --- follow-ups -------------------------------------------------------------


def seed_sent(email, *, days_ago, status=sheet.SENT, touch=1, company="Fake Co"):
    sent_at = (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .astimezone()
        .isoformat(timespec="seconds")
    )
    return sheet.append(
        {
            "company": company,
            "contact_email": email,
            "status": status,
            "touch_number": touch,
            "sent_at": sent_at,
            "thread_id": "t-" + email,
            "draft_subject": "First touch",
            "draft_body": "First touch body",
        }
    )


def test_replied_prospect_is_excluded_from_followup(env):
    seed_sent("quiet@example.com", days_ago=9)
    seed_sent("replied@example.com", days_ago=9, status=sheet.REPLIED)

    eligible = {r["contact_email"] for r in followup.eligible(sheet.read_rows())}

    assert eligible == {"quiet@example.com"}


def test_reply_on_any_touch_retires_every_touch_for_that_contact(env):
    # Touch 1 looks overdue, but touch 2 to the same person already got a reply.
    seed_sent("mixed@example.com", days_ago=30, touch=1)
    seed_sent("mixed@example.com", days_ago=20, touch=2, status=sheet.REPLIED)
    seed_sent("quiet@example.com", days_ago=30, touch=1)

    eligible = {r["contact_email"] for r in followup.eligible(sheet.read_rows())}

    assert "mixed@example.com" not in eligible
    assert eligible == {"quiet@example.com"}


def test_followup_skips_recent_sends_and_existing_next_touches(env):
    seed_sent("recent@example.com", days_ago=1)
    seed_sent("queued@example.com", days_ago=9, touch=1)
    sheet.append(
        {"contact_email": "queued@example.com", "status": sheet.DRAFT, "touch_number": 2}
    )
    seed_sent("due@example.com", days_ago=9)

    eligible = {r["contact_email"] for r in followup.eligible(sheet.read_rows())}

    assert eligible == {"due@example.com"}


def test_followup_sends_touch_two_with_the_first_email_as_context(env, monkeypatch):
    seed_sent("due@example.com", days_ago=9)
    seen = {}

    def fake_draft(prospect, facts, notes, feedback=None, previous=None):
        seen["previous"] = previous
        seen["touch"] = prospect["touch_number"]
        return GOOD

    monkeypatch.setattr(llm, "draft_email", fake_draft)
    monkeypatch.setattr(llm, "validate_draft", lambda *a, **k: PASS)
    sends = mock_gmail(monkeypatch)

    queue = followup.queue_next_touches(sheet.read_rows())
    final = graph.run(entry="next_prospect", queue=queue, facts=FACTS)

    assert seen["touch"] == 2
    assert seen["previous"]["subject"] == "First touch", "not a cold restart"
    assert sends == ["due@example.com"]
    assert final["sent"] == 1

    touch_two = [r for r in rows_by_email("due@example.com") if r["touch_number"] == 2][0]
    assert touch_two["status"] == sheet.SENT


def test_followup_send_failure_parks_only_that_row(env, monkeypatch):
    seed_sent("due@example.com", days_ago=9)
    mock_llm(monkeypatch, [])
    mock_gmail(monkeypatch, fail_on={"due@example.com"})

    queue = followup.queue_next_touches(sheet.read_rows())
    graph.run(entry="next_prospect", queue=queue, facts=FACTS)

    rows = rows_by_email("due@example.com")
    assert rows[0]["status"] == sheet.SENT, "the original touch is untouched"
    assert rows[1]["status"] == sheet.NEEDS_REVIEW
    assert "send failed" in rows[1]["last_error"]


# --- the deterministic half of validate ------------------------------------


@pytest.mark.parametrize(
    "subject,body,expected",
    [
        ("Sponsoring [COMPANY] this year", GOOD.body, "placeholder"),
        ("Hi", GOOD.body, "Subject is"),
        (GOOD.subject, "Too short.", "Body is"),
    ],
)
def test_static_checks_catch_obvious_breakage(subject, body, expected):
    errors = llm._static_checks(subject, body, {"company": "Acme Robotics"}, FACTS)
    assert any(expected in error for error in errors), errors


def test_static_checks_pass_a_clean_draft():
    assert (
        llm._static_checks(GOOD.subject, GOOD.body, {"company": "Acme Robotics"}, FACTS)
        == []
    )


def test_missing_sender_identification_is_caught():
    body = GOOD.body.replace("Sample Sender", "someone")
    errors = llm._static_checks(GOOD.subject, body, {"company": "Acme Robotics"}, FACTS)
    assert any("does not identify the sender" in error for error in errors)


# --- Ollama HTTP layer ------------------------------------------------------
# These mock urlopen inside llm, so no request leaves the process.

import json as _json  # noqa: E402
from urllib.error import URLError  # noqa: E402

TAGS = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:14b"}]}
DRAFT_JSON = _json.dumps({"subject": "Sponsoring Fake Co at a placeholder event",
                          "body": "Hi Pat, Sample Sender here. Would you be open to a call?"})


class _FakeResponse:
    def __init__(self, payload):
        self._raw = _json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_http(monkeypatch, responses):
    """Serve canned JSON bodies in order and record every request."""
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(
            {
                "url": request.full_url,
                "payload": _json.loads(request.data.decode("utf-8")) if request.data else None,
            }
        )
        return _FakeResponse(responses.pop(0))

    monkeypatch.setattr(llm, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://localhost:11434")
    monkeypatch.setattr(config, "OLLAMA_DRAFT_MODEL", "llama3.1")
    monkeypatch.setattr(config, "OLLAMA_VALIDATE_MODEL", "qwen2.5:14b")


def test_ollama_structured_call_returns_a_draft(ollama, monkeypatch):
    calls = _fake_http(monkeypatch, [TAGS, {"message": {"content": DRAFT_JSON}}])

    draft = llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")

    assert isinstance(draft, llm.Draft)
    assert draft.subject.startswith("Sponsoring Fake Co")
    assert calls[0]["url"].endswith("/api/tags")
    chat = calls[1]
    assert chat["url"].endswith("/api/chat")
    assert chat["payload"]["model"] == "llama3.1:8b"  # bare name resolved to the pulled tag
    assert chat["payload"]["stream"] is False
    assert "subject" in chat["payload"]["format"]["properties"]  # schema-constrained


def test_ollama_validation_uses_its_own_model(ollama, monkeypatch):
    verdict_json = _json.dumps({"passed": True, "errors": [], "unsupported_claims": []})
    calls = _fake_http(monkeypatch, [TAGS, {"message": {"content": verdict_json}}])

    verdict = llm.validate_draft(GOOD.subject, GOOD.body, {"company": "Acme Robotics"}, FACTS, "")

    assert verdict.passed
    assert calls[1]["payload"]["model"] == "qwen2.5:14b"


def test_ollama_malformed_json_is_retried_then_recovers(ollama, monkeypatch):
    calls = _fake_http(
        monkeypatch,
        [
            TAGS,
            {"message": {"content": "Sure! Here is your email: subject = hello"}},
            {"message": {"content": DRAFT_JSON}},
        ],
    )

    draft = llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")

    assert isinstance(draft, llm.Draft)
    assert len(calls) == 3  # tags, bad chat, retried chat
    retry_messages = calls[2]["payload"]["messages"]
    assert any("not valid JSON matching the schema" in m["content"] for m in retry_messages)


def test_ollama_gives_up_after_two_bad_responses(ollama, monkeypatch):
    junk = {"message": {"content": "still not json"}}
    calls = _fake_http(monkeypatch, [TAGS, junk, junk])

    with pytest.raises(RuntimeError, match="did not return valid Draft JSON"):
        llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")

    assert len(calls) == 3  # tags plus exactly two attempts, no third


def test_ollama_model_not_pulled_names_the_pull_command(ollama, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_DRAFT_MODEL", "mistral-large")
    _fake_http(monkeypatch, [TAGS])

    with pytest.raises(RuntimeError, match="ollama pull mistral-large"):
        llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")


def test_ollama_down_is_a_readable_error(ollama, monkeypatch):
    def refuse(request, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(llm, "urlopen", refuse)

    with pytest.raises(ConnectionError, match="ollama serve"):
        llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")


def test_unknown_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gpt-at-home")
    with pytest.raises(RuntimeError, match="Unknown LLM_PROVIDER"):
        llm.draft_email({"company": "Fake Co"}, FACTS, "no data found")
