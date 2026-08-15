/**
 * Answering the agent, rather than permitting it.
 *
 * Claude's AskUserQuestion tool reaches AIOps down the same pipe as every other
 * tool call, so for a while it rendered as a bare Accept/Deny with the question
 * itself invisible — and accepting it told the model "the user did not answer",
 * which is a stall dressed up as progress. The card that replaces that is a
 * form, and a form has rules: every question answered, nothing chosen that was
 * not offered, single-select meaning single.
 *
 * Those rules live here rather than inside the component because the server
 * enforces the same ones and rejects the request if they disagree — so they are
 * worth being able to test without rendering anything. The component owns the
 * draft; this owns what the draft means.
 */
import type { ApprovalAnswer, ApprovalQuestion } from "./types";

/** What a person has entered for one question, before it is any good. */
export type QuestionDraft = {
  /** Option labels ticked. Single-select keeps at most one. */
  chosen: string[];
  /** The "Other" box. Whitespace-only counts as empty. */
  other: string;
};

export type Draft = Record<string, QuestionDraft>;

/** A blank draft: one empty entry per question, keyed by the question's text. */
export function emptyDraft(questions: ApprovalQuestion[]): Draft {
  const draft: Draft = {};
  for (const question of questions) draft[question.question] = { chosen: [], other: "" };
  return draft;
}

/**
 * Tick or untick one option.
 *
 * Single-select replaces rather than adds — the radio behaviour — and clearing
 * the "Other" box with it, because a question that takes one answer cannot hold
 * a choice and a sentence at once and leaving both on screen invites a form
 * that submits something the person did not mean.
 */
export function toggleOption(
  draft: Draft,
  question: ApprovalQuestion,
  label: string,
): Draft {
  const current = draft[question.question] ?? { chosen: [], other: "" };
  if (!question.multi_select) {
    return { ...draft, [question.question]: { chosen: [label], other: "" } };
  }
  const chosen = current.chosen.includes(label)
    ? current.chosen.filter((c) => c !== label)
    : [...current.chosen, label];
  return { ...draft, [question.question]: { ...current, chosen } };
}

/**
 * Type in the "Other" box.
 *
 * On a single-select question that clears the ticked option for the same reason
 * ticking clears the box: whichever the person touched last is the answer they
 * meant.
 */
export function setOther(draft: Draft, question: ApprovalQuestion, other: string): Draft {
  const current = draft[question.question] ?? { chosen: [], other: "" };
  const chosen = question.multi_select || !other.trim() ? current.chosen : [];
  return { ...draft, [question.question]: { chosen, other } };
}

/** Whether one question has anything in it yet. */
export function isAnswered(draft: Draft, question: ApprovalQuestion): boolean {
  const current = draft[question.question];
  if (!current) return false;
  return current.chosen.length > 0 || current.other.trim().length > 0;
}

/**
 * The first thing wrong with the draft, or null when it is ready to send.
 *
 * One message, not a list: the card shows it next to the submit button, and a
 * person fixing a form fixes one thing at a time.
 */
export function validate(questions: ApprovalQuestion[], draft: Draft): string | null {
  if (questions.length === 0) return "There is nothing to answer here.";
  for (const question of questions) {
    const current = draft[question.question] ?? { chosen: [], other: "" };
    const offered = new Set(question.options.map((o) => o.label));
    for (const label of current.chosen) {
      if (!offered.has(label)) {
        return `“${label}” is not one of the choices for “${short(question)}”.`;
      }
    }
    const other = current.other.trim();
    const total = current.chosen.length + (other ? 1 : 0);
    if (total === 0) {
      return `“${short(question)}” still needs an answer.`;
    }
    if (total > 1 && !question.multi_select) {
      return `“${short(question)}” takes only one answer.`;
    }
  }
  return null;
}

/**
 * The draft as the API takes it.
 *
 * Only meaningful once `validate` returns null; callers are expected to check
 * first, and the server checks again regardless.
 */
export function toAnswers(questions: ApprovalQuestion[], draft: Draft): ApprovalAnswer[] {
  return questions.map((question) => {
    const current = draft[question.question] ?? { chosen: [], other: "" };
    const other = current.other.trim();
    return {
      question: question.question,
      options: [...current.chosen],
      text: other ? other : null,
    };
  });
}

/** A question's own words, cut short enough to sit inside a sentence. */
function short(question: ApprovalQuestion): string {
  const text = question.header?.trim() || question.question.trim();
  return text.length > 60 ? `${text.slice(0, 59)}…` : text;
}
