"""Draft the email, then judge it. Two structured-output calls, one provider.

Default provider is a local Ollama instance (no API key, nothing leaves the
machine). Anthropic stays available as a fallback via LLM_PROVIDER=anthropic;
its key still comes from ANTHROPIC_API_KEY and is never written to disk.

Both providers return the same Draft / Verdict Pydantic objects, so graph.py
never learns which one answered.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Type, TypeVar
from urllib.error import URLError
from urllib.request import Request, urlopen

import anthropic
from pydantic import BaseModel, Field

from sponsor_agent import config
from sponsor_agent.research import NO_DATA

T = TypeVar("T", bound=BaseModel)

MIN_BODY, MAX_BODY = 350, 2500
MIN_SUBJECT, MAX_SUBJECT = 12, 90

# Local models are slow and schema-sloppy next to a hosted one; both numbers
# exist for that, not for Anthropic.
OLLAMA_TIMEOUT = 300
OLLAMA_JSON_ATTEMPTS = 2

_PLACEHOLDER = re.compile(
    r"\[[^\]\n]{2,60}\]"
    r"|\{\{[^}\n]{1,60}\}\}"
    r"|<[A-Za-z _]{2,40}>"
    r"|\b(?:TODO|TBD|XXX+|FIXME|LOREM IPSUM|YOUR NAME|INSERT [A-Z ]{2,30})\b",
    re.I,
)


class Draft(BaseModel):
    subject: str = Field(description="Email subject line. No placeholders.")
    body: str = Field(description="Plain-text email body, including sign-off.")


class Verdict(BaseModel):
    passed: bool = Field(description="True only if every check below is clean.")
    errors: list[str] = Field(
        default_factory=list,
        description="Specific, actionable problems the writer must fix. Empty if passed.",
    )
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Verbatim phrases from the draft not traceable to the fact sheet or research notes.",
    )


# --- fact sheet ------------------------------------------------------------


def load_fact_sheet(path=None) -> dict[str, Any]:
    """JSON -> the parsed object. Markdown/text -> {"raw_markdown": text}."""
    path = path or config.FACT_SHEET
    if not path.exists():
        raise FileNotFoundError(
            f"Fact sheet not found at {path}. Copy config/fact_sheet.example.json to "
            f"config/fact_sheet.json and fill in your real numbers."
        )
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return {"raw_markdown": text}


# --- Ollama (default provider) ---------------------------------------------


def _ollama_call(path: str, payload: dict | None = None, timeout: int = 10) -> dict:
    """One JSON request to the local Ollama daemon. Down means a clear message."""
    url = config.OLLAMA_HOST.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (URLError, OSError) as exc:
        raise ConnectionError(
            f"Cannot reach Ollama at {config.OLLAMA_HOST} ({exc}). Start it with "
            f"`ollama serve`, or set LLM_PROVIDER=anthropic to use the API instead."
        ) from exc
    except ValueError as exc:
        raise RuntimeError(f"Ollama returned something that is not JSON: {exc}") from exc


def installed_models() -> list[str]:
    """Model names Ollama actually has pulled, in its own order."""
    data = _ollama_call("/api/tags")
    return [m["name"] for m in data.get("models", []) if m.get("name")]


def resolve_model(configured: str, purpose: str) -> str:
    """Configured name if it is pulled, else the first installed one. Never a guess.

    A bare name matches a tagged one, so OLLAMA_DRAFT_MODEL=llama3.1 finds
    llama3.1:8b. Nothing usable means an error naming the pull command.
    """
    installed = installed_models()
    if not installed:
        raise RuntimeError(
            f"Ollama is running at {config.OLLAMA_HOST} but has no models pulled. "
            f"Pull one (for example `ollama pull llama3.1`) and set OLLAMA_DRAFT_MODEL."
        )

    if not configured:
        return installed[0]
    if configured in installed:
        return configured
    tagged = [name for name in installed if name.split(":", 1)[0] == configured]
    if tagged:
        return tagged[0]

    raise RuntimeError(
        f"Ollama model '{configured}' ({purpose}) is not pulled on {config.OLLAMA_HOST}. "
        f"Run: ollama pull {configured}\n"
        f"Installed right now: {', '.join(installed)} (see `ollama list`)."
    )


def _ollama_structured(system: str, user: str, model: Type[T], max_tokens: int, purpose: str) -> T:
    """Schema-constrained /api/chat, retried once when the JSON comes back wrong."""
    configured = (
        config.OLLAMA_DRAFT_MODEL if purpose == "draft" else config.OLLAMA_VALIDATE_MODEL
    )
    name = resolve_model(configured, purpose)
    schema = model.model_json_schema()

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_error = ""

    for _ in range(OLLAMA_JSON_ATTEMPTS):
        response = _ollama_call(
            "/api/chat",
            {
                "model": name,
                "messages": messages,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0.3, "num_predict": max_tokens},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        content = (response.get("message") or {}).get("content", "")
        try:
            return model.model_validate_json(content)
        except ValueError as exc:  # JSON decode error or pydantic ValidationError
            last_error = str(exc)[:400]
            messages += [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your last response was not valid JSON matching the schema. "
                        f"The parser said: {last_error}\n"
                        f"Required schema:\n{json.dumps(schema)}\n"
                        "Reply with ONLY that JSON object. No prose, no markdown fence."
                    ),
                },
            ]

    raise RuntimeError(
        f"Ollama model {name} did not return valid {model.__name__} JSON in "
        f"{OLLAMA_JSON_ATTEMPTS} attempts. Last parser error: {last_error}"
    )


# --- Anthropic (fallback provider) -----------------------------------------


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    return anthropic.Anthropic(api_key=key)


def _anthropic_structured(
    system: str, user: str, model: Type[T], max_tokens: int, purpose: str
) -> T:
    response = _client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        tools=[
            {
                "name": "emit",
                "description": f"Return the result as {model.__name__}.",
                "input_schema": model.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": "emit"},
        messages=[{"role": "user", "content": user}],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return model.model_validate(block.input)
    raise RuntimeError("Model returned no structured output.")


# --- dispatch --------------------------------------------------------------


def _client_for(provider: str):
    """Pick the structured-output backend. Same signature either way."""
    provider = (provider or "").strip().lower()
    if provider == "ollama":
        return _ollama_structured
    if provider == "anthropic":
        return _anthropic_structured
    raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}'. Use 'ollama' or 'anthropic'.")


def _structured(system: str, user: str, model: Type[T], max_tokens: int, purpose: str) -> T:
    return _client_for(config.LLM_PROVIDER)(system, user, model, max_tokens, purpose)


# --- personalize -----------------------------------------------------------

DRAFT_SYSTEM = """You write short, plain, specific sponsorship outreach emails.

ABSOLUTE RULE - GROUNDING:
You may use ONLY two sources of fact: (1) the SPONSORSHIP FACT SHEET and
(2) the RESEARCH NOTES, both given below. You must not invent, estimate,
extrapolate, round, or "reasonably assume" ANY statistic, audience number,
reach figure, past-sponsor name, award, growth claim, testimonial, date,
price, or detail about either party that is not literally present in those
two sources. If a number would make the pitch stronger and you do not have
it, leave it out. Inventing a fact is a total failure of this task - worse
than a bland email.

If the research notes say "no data found", that is normal and fine. Write a
short, honest, non-personalised-but-specific email built from the fact sheet
alone. Do NOT fake familiarity with the company, do not guess what they do,
and do not write things like "I have been following your work" unless the
research notes actually support it.

STYLE:
- Plain text. No markdown, no bullet characters, no emoji.
- 120-200 words. Short paragraphs. Human, direct, no hype adjectives.
- Subject line: 12-90 characters. Never longer.
- Never leave a placeholder, a bracket, or a blank to fill in later. A draft
  containing "[Your Name]", "[Company]" or any other bracketed slot is rejected.

The email must contain all four of these:
1. The recipient organisation's name, written out verbatim in the body exactly
   as it appears in PROSPECT.company, at least once. Not "your company", not
   "your team", not "your organisation" - the actual name. An email that never
   names them is rejected. Open with something concrete about them from the
   research notes; if there are none, give a plain honest reason for writing.
2. The actual ask, stated clearly, exactly as the fact sheet defines it.
3. A clear way to reply or continue (a direct question, or the reply-to address).
4. A sign-off that closes the email, in exactly this shape, filled in from the
   fact sheet's own values:

     Best,
     <sender_name>
     <sender_role>, <organisation name>
     <reply_to>

   These values are given to you. Never a bracketed slot, and never the
   recipient's contact name here - this block is the sender, not them.
"""


def draft_email(
    prospect: dict,
    facts: dict,
    research_notes: str,
    feedback: list[str] | None = None,
    previous: dict | None = None,
) -> Draft:
    parts = [
        "SPONSORSHIP FACT SHEET (authoritative - the only source of claims about us):",
        json.dumps(facts, indent=2, ensure_ascii=False),
        "",
        "PROSPECT:",
        json.dumps(
            {k: prospect.get(k, "") for k in ("company", "contact_name", "contact_email", "website")},
            indent=2,
            ensure_ascii=False,
        ),
        "",
        "RESEARCH NOTES (the only source of claims about them):",
        research_notes or NO_DATA,
    ]
    if previous:
        parts += [
            "",
            "THIS IS A FOLLOW-UP, NOT A COLD EMAIL.",
            "A previous email was already sent on "
            + str(previous.get("sent_at", "an earlier date"))
            + " and has not been answered. Reference it briefly and politely, add one new"
            " useful thing, and keep it shorter than the original. Do not restart from"
            " scratch and do not repeat the text of the original email.",
            "PREVIOUS SUBJECT: " + str(previous.get("subject", "")),
            "PREVIOUS BODY:",
            str(previous.get("body", "")),
        ]
    if feedback:
        parts += [
            "",
            "YOUR PREVIOUS DRAFT WAS REJECTED. Fix exactly these problems, change nothing else:",
            *(f"- {f}" for f in feedback),
        ]
    return _structured(DRAFT_SYSTEM, "\n".join(parts), Draft, max_tokens=4096, purpose="draft")


# --- validate --------------------------------------------------------------

VALIDATE_SYSTEM = """You are a strict pre-send reviewer for outreach email drafts.
You are not the writer. Assume the draft is wrong until it proves otherwise.

Fail the draft if ANY of these is true:
1. It contains a placeholder, a bracket, or anything the sender still has to fill in.
2. It is missing any of: a concrete opener, the actual ask, a way to reply, or
   the sender name / role / organisation.
3. ANY factual claim in it is not traceable to the fact sheet or the research
   notes. This is the most important check. Walk every number, name, statistic,
   date, credential and claim about either party back to its source. Anything
   you cannot trace goes into unsupported_claims verbatim AND fails the draft.
   Generic politeness and opinion are fine; an asserted fact is not.
4. It claims familiarity with the recipient that the research notes do not support.

Research notes reading "no data found" are NOT a failure. A plain email built
only from the fact sheet is acceptable and should pass.

Every entry in errors must tell the writer precisely what to change.
"""


def _static_checks(subject: str, body: str, prospect: dict, facts: dict) -> list[str]:
    """Cheap deterministic checks. No API call needed to catch the obvious breakage."""
    errors: list[str] = []
    subject, body = str(subject or ""), str(body or "")

    if not subject.strip() or not body.strip():
        return ["Subject or body is empty."]

    found = _PLACEHOLDER.findall(subject + "\n" + body)
    if found:
        errors.append(f"Unfilled placeholder text present: {sorted(set(found))[:5]}")

    if not MIN_SUBJECT <= len(subject) <= MAX_SUBJECT:
        errors.append(f"Subject is {len(subject)} chars; must be {MIN_SUBJECT}-{MAX_SUBJECT}.")
    if not MIN_BODY <= len(body) <= MAX_BODY:
        errors.append(f"Body is {len(body)} chars; must be {MIN_BODY}-{MAX_BODY}.")

    company = str(prospect.get("company", "") or "")
    tokens = [t for t in re.split(r"\W+", company) if len(t) > 3]
    if tokens and not any(t.lower() in body.lower() for t in tokens):
        errors.append(f"Body never names the recipient organisation ({company}).")

    sender = str(facts.get("sender_name", "") or "")
    sender_tokens = [t for t in re.split(r"\W+", sender) if len(t) > 2]
    if sender_tokens and not any(t.lower() in body.lower() for t in sender_tokens):
        errors.append(f"Body does not identify the sender ({sender}).")

    reply_to = str(facts.get("reply_to", "") or "")
    has_prompt = re.search(r"\breply\b|\bget back\b|\bhappy to\b|\bwould you\b|\?", body, re.I)
    if not has_prompt and (not reply_to or reply_to.lower() not in body.lower()):
        errors.append("Body gives the recipient no clear way to reply.")

    return errors


def validate_draft(
    subject: str, body: str, prospect: dict, facts: dict, research_notes: str
) -> Verdict:
    """Deterministic checks first; the model is only asked once those pass."""
    errors = _static_checks(subject, body, prospect, facts)
    if errors:
        return Verdict(passed=False, errors=errors)

    user = "\n".join(
        [
            "SPONSORSHIP FACT SHEET:",
            json.dumps(facts, indent=2, ensure_ascii=False),
            "",
            "RESEARCH NOTES:",
            research_notes or NO_DATA,
            "",
            "PROSPECT: " + str(prospect.get("company", "")),
            "",
            "DRAFT SUBJECT: " + str(subject),
            "DRAFT BODY:",
            str(body),
        ]
    )
    return _structured(VALIDATE_SYSTEM, user, Verdict, max_tokens=1500, purpose="validate")
