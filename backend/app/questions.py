"""Claude's `AskUserQuestion` tool: reading it, and answering it.

The tool arrives through the permission-prompt path like any other tool call,
which is why it used to render as a bare Accept/Deny with the questions
invisible. But it is not a permission at all — the agent is not asking whether
it may do something, it is asking a person a multiple-choice question and
waiting for the reply.

The reply contract was established by driving the real CLI (Claude Code 2.1.x)
through the real bridge against a stub approvals endpoint:

* ``{"behavior": "allow", "updatedInput": {<the original input>, "answers": {...}}}``
  where ``answers`` is keyed by the *question text* and valued by the chosen
  option label. The tool then returns to the model:
  ``The user answered: "<question>"="<label>". Read the answers carefully …``
  and the turn continues with those answers.
* A ``multiSelect`` question takes a JSON list of labels, which the tool renders
  comma-joined.
* Free text that is not one of the offered labels is passed through verbatim —
  which is what makes an "Other" box possible.
* ``allow`` with no ``answers`` returns "The user did not answer the questions."
  That is exactly the dead end a generic Accept button produced.
* ``deny`` returns the message verbatim as an *error* tool result.

Codex has no equivalent tool, so none of this applies to it; the caller gates on
the provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The Claude tool this module is about. Matched exactly — a tool merely
#: *containing* the name is somebody else's.
TOOL_NAME = "AskUserQuestion"

#: Longest summary we will build from a question's text. The column takes 4000,
#: but a summary is a list row and a notification title.
SUMMARY_LIMIT = 160

#: Bounds on what we will accept as a question set. A malformed or hostile
#: payload should degrade to "an ordinary tool approval", never to a UI that
#: tries to render ten thousand radio buttons.
MAX_QUESTIONS = 20
MAX_OPTIONS = 40


@dataclass(frozen=True)
class Option:
    """One offered answer. The description is where the decision lives."""

    label: str
    description: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "description": self.description}


@dataclass(frozen=True)
class Question:
    header: str | None
    question: str
    multi_select: bool
    options: tuple[Option, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "header": self.header,
            "question": self.question,
            "multi_select": self.multi_select,
            "options": [option.as_dict() for option in self.options],
        }


class AnswerError(ValueError):
    """An answer set that cannot be sent to the agent, and why."""


def is_question_tool(provider: str | None, tool_name: str | None) -> bool:
    """Whether this call is Claude's question tool.

    Gated on the provider as well as the name: Codex has no such tool, and a
    Codex tool that happened to share the name is not this contract.
    """
    return (provider or "") == "claude" and (tool_name or "") == TOOL_NAME


def parse_questions(request: Any) -> list[Question]:
    """The questions in a raw `AskUserQuestion` input, or [] if it is not one.

    Deliberately total: anything unrecognised yields no questions, and the
    caller falls back to the ordinary tool-approval card rather than raising in
    the middle of an agent's turn.
    """
    if not isinstance(request, dict):
        return []
    raw = request.get("questions")
    if not isinstance(raw, list):
        return []

    parsed: list[Question] = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        text = item.get("question")
        if not isinstance(text, str) or not text.strip():
            # A question with nothing to read cannot be answered by a person,
            # and its text is the key the answer travels under.
            continue
        options: list[Option] = []
        raw_options = item.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options[:MAX_OPTIONS]:
                if isinstance(opt, dict):
                    label = opt.get("label")
                    description = opt.get("description")
                    if isinstance(label, str) and label.strip():
                        options.append(
                            Option(
                                label=label,
                                description=description
                                if isinstance(description, str) and description.strip()
                                else None,
                            )
                        )
                elif isinstance(opt, str) and opt.strip():
                    # Not a shape the CLI has produced, but a bare string list
                    # is the obvious degradation and costs nothing to accept.
                    options.append(Option(label=opt))
        header = item.get("header")
        parsed.append(
            Question(
                header=header if isinstance(header, str) and header.strip() else None,
                question=text,
                multi_select=bool(item.get("multiSelect")),
                options=tuple(options),
            )
        )

    # Two questions with the same text cannot be told apart in the answers
    # object the tool expects, so the set is unusable as a question set.
    seen = {q.question for q in parsed}
    if len(seen) != len(parsed):
        return []
    return parsed


def summarise(questions: list[Question]) -> str | None:
    """What the lists and notifications say instead of "AskUserQuestion".

    The first question's own words, because that is what the person is being
    asked; the count is appended when there is more than one so a notification
    does not understate what is waiting.
    """
    if not questions:
        return None
    text = " ".join(questions[0].question.split())
    extra = len(questions) - 1
    suffix = f" (+{extra} more)" if extra > 0 else ""
    room = SUMMARY_LIMIT - len(suffix)
    if len(text) > room:
        text = text[: max(0, room - 1)].rstrip() + "…"
    return f"{text}{suffix}"


@dataclass(frozen=True)
class Answer:
    """One person's reply to one question."""

    question: str
    #: Labels chosen from the offered options.
    options: tuple[str, ...] = ()
    #: The "Other" box. Passed to the model verbatim.
    text: str | None = None


def validate_answers(questions: list[Question], answers: list[Answer]) -> dict[str, Any]:
    """Turn a person's choices into the `answers` object the tool expects.

    Raises `AnswerError` rather than silently dropping anything: an answer that
    half-arrives is worse than a rejected form, because the model would act on
    the half.
    """
    if not questions:
        raise AnswerError("This approval has no questions to answer.")

    by_text: dict[str, list[Answer]] = {}
    for answer in answers:
        by_text.setdefault(answer.question, []).append(answer)

    known = {q.question for q in questions}
    unknown = sorted(set(by_text) - known)
    if unknown:
        raise AnswerError(f"Not a question that was asked: {unknown[0]!r}")

    built: dict[str, Any] = {}
    for question in questions:
        got = by_text.get(question.question) or []
        if len(got) > 1:
            raise AnswerError(f"Answered twice: {question.question!r}")
        if not got:
            raise AnswerError(f"Still unanswered: {question.question!r}")
        answer = got[0]

        free = (answer.text or "").strip()
        chosen = [label for label in answer.options]
        offered = {option.label for option in question.options}
        for label in chosen:
            if label not in offered:
                raise AnswerError(
                    f"{label!r} was not offered for {question.question!r}. "
                    "Use the other box for an answer of your own."
                )
        if len(set(chosen)) != len(chosen):
            raise AnswerError(f"The same option was chosen twice for {question.question!r}")

        total = len(chosen) + (1 if free else 0)
        if total == 0:
            raise AnswerError(f"Still unanswered: {question.question!r}")
        if total > 1 and not question.multi_select:
            raise AnswerError(
                f"{question.question!r} takes one answer, but {total} were given."
            )

        values = [*chosen, *([free] if free else [])]
        # Verified against the CLI: a multiSelect question takes a list (which
        # the tool renders comma-joined) and a single-select one takes a plain
        # string. Sending a list to a single-select question is not something
        # the contract was observed to accept, so it is not sent.
        built[question.question] = values if question.multi_select else values[0]

    return built


def build_updated_input(request: Any, answers: dict[str, Any]) -> dict[str, Any]:
    """The `updatedInput` the bridge hands back to the CLI.

    The *whole* original input plus `answers`, which is what was verified to
    work — the tool re-validates its own input, so a reply carrying only the
    answers is not worth the risk.
    """
    base = dict(request) if isinstance(request, dict) else {}
    base["answers"] = answers
    return base
