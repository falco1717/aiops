import { describe, expect, it } from "vitest";
import type { ApprovalQuestion } from "./types";
import {
  emptyDraft,
  isAnswered,
  setOther,
  toAnswers,
  toggleOption,
  validate,
} from "./questions";

/**
 * The two questions from the approval that started this — a real production row
 * where the option labels were nearly interchangeable and the descriptions
 * carried the whole decision.
 */
const HDR: ApprovalQuestion = {
  header: "Device HDR",
  question: "Does your TV or receiver handle HDR tone mapping well?",
  multi_select: false,
  options: [
    { label: "Yes, keep HDR", description: "Pass HDR through untouched." },
    { label: "No, tone map to SDR", description: "Convert during transcode." },
    { label: "Only for 4K", description: "Keep HDR on 4K sources only." },
  ],
};

const PROFILES: ApprovalQuestion = {
  header: "Profiles",
  question: "Which quality profiles should Radarr and Sonarr prefer?",
  multi_select: true,
  options: [
    { label: "Remux", description: "Untouched disc rip." },
    { label: "Bluray-1080p", description: "A good default." },
    { label: "WEBDL-2160p", description: "Streaming-sourced 4K." },
  ],
};

const BOTH = [HDR, PROFILES];

describe("emptyDraft", () => {
  it("makes one blank entry per question, keyed by its wording", () => {
    expect(emptyDraft(BOTH)).toEqual({
      [HDR.question]: { chosen: [], other: "" },
      [PROFILES.question]: { chosen: [], other: "" },
    });
  });
});

describe("choosing", () => {
  it("replaces rather than adds on a single-select question", () => {
    let draft = emptyDraft(BOTH);
    draft = toggleOption(draft, HDR, "Yes, keep HDR");
    draft = toggleOption(draft, HDR, "Only for 4K");
    expect(draft[HDR.question].chosen).toEqual(["Only for 4K"]);
  });

  it("accumulates on a multi-select question, and unticks", () => {
    let draft = emptyDraft(BOTH);
    draft = toggleOption(draft, PROFILES, "Remux");
    draft = toggleOption(draft, PROFILES, "WEBDL-2160p");
    expect(draft[PROFILES.question].chosen).toEqual(["Remux", "WEBDL-2160p"]);
    draft = toggleOption(draft, PROFILES, "Remux");
    expect(draft[PROFILES.question].chosen).toEqual(["WEBDL-2160p"]);
  });

  it("does not disturb the other question", () => {
    let draft = emptyDraft(BOTH);
    draft = toggleOption(draft, HDR, "Only for 4K");
    expect(draft[PROFILES.question]).toEqual({ chosen: [], other: "" });
  });
});

describe("the other box", () => {
  it("clears a single-select choice, because only one of them can be the answer", () => {
    let draft = emptyDraft(BOTH);
    draft = toggleOption(draft, HDR, "Only for 4K");
    draft = setOther(draft, HDR, "Only on the projector");
    expect(draft[HDR.question]).toEqual({ chosen: [], other: "Only on the projector" });
  });

  it("and is cleared by choosing one, whichever was touched last", () => {
    let draft = setOther(emptyDraft(BOTH), HDR, "something of my own");
    draft = toggleOption(draft, HDR, "Yes, keep HDR");
    expect(draft[HDR.question]).toEqual({ chosen: ["Yes, keep HDR"], other: "" });
  });

  it("sits alongside ticked options on a multi-select question", () => {
    let draft = toggleOption(emptyDraft(BOTH), PROFILES, "Remux");
    draft = setOther(draft, PROFILES, "and DVD for the old stuff");
    expect(draft[PROFILES.question]).toEqual({
      chosen: ["Remux"],
      other: "and DVD for the old stuff",
    });
  });

  it("emptying it does not wipe a single-select choice", () => {
    let draft = toggleOption(emptyDraft(BOTH), HDR, "Only for 4K");
    draft = setOther(draft, HDR, "  ");
    expect(draft[HDR.question].chosen).toEqual(["Only for 4K"]);
  });
});

describe("isAnswered", () => {
  it("counts a ticked option", () => {
    expect(isAnswered(toggleOption(emptyDraft(BOTH), HDR, "Only for 4K"), HDR)).toBe(true);
  });
  it("counts free text", () => {
    expect(isAnswered(setOther(emptyDraft(BOTH), HDR, "elsewhere"), HDR)).toBe(true);
  });
  it("does not count whitespace", () => {
    expect(isAnswered(setOther(emptyDraft(BOTH), HDR, "   "), HDR)).toBe(false);
  });
  it("is false for an untouched question", () => {
    expect(isAnswered(emptyDraft(BOTH), PROFILES)).toBe(false);
  });
});

describe("validate", () => {
  it("refuses an empty draft, naming the first question still needing one", () => {
    expect(validate(BOTH, emptyDraft(BOTH))).toContain("Device HDR");
  });

  it("still refuses when only one of two is answered", () => {
    const draft = toggleOption(emptyDraft(BOTH), HDR, "Only for 4K");
    expect(validate(BOTH, draft)).toContain("Profiles");
  });

  it("passes once every question has something", () => {
    let draft = toggleOption(emptyDraft(BOTH), HDR, "Only for 4K");
    draft = toggleOption(draft, PROFILES, "Remux");
    expect(validate(BOTH, draft)).toBeNull();
  });

  it("accepts free text as a whole answer", () => {
    let draft = setOther(emptyDraft(BOTH), HDR, "neither, it is a projector");
    draft = setOther(draft, PROFILES, "whatever fits under 20GB");
    expect(validate(BOTH, draft)).toBeNull();
  });

  it("rejects an option that was never offered", () => {
    const draft = {
      [HDR.question]: { chosen: ["Dolby Vision only"], other: "" },
      [PROFILES.question]: { chosen: ["Remux"], other: "" },
    };
    expect(validate(BOTH, draft)).toContain("not one of the choices");
  });

  it("rejects two answers to a single-select question", () => {
    const draft = {
      [HDR.question]: { chosen: ["Yes, keep HDR", "Only for 4K"], other: "" },
      [PROFILES.question]: { chosen: ["Remux"], other: "" },
    };
    expect(validate(BOTH, draft)).toContain("only one answer");
  });

  it("counts the other box against a single-select question's one answer", () => {
    const draft = {
      [HDR.question]: { chosen: ["Only for 4K"], other: "or maybe not" },
      [PROFILES.question]: { chosen: ["Remux"], other: "" },
    };
    expect(validate(BOTH, draft)).toContain("only one answer");
  });

  it("allows several answers to a multi-select question", () => {
    const draft = {
      [HDR.question]: { chosen: ["Only for 4K"], other: "" },
      [PROFILES.question]: { chosen: ["Remux", "Bluray-1080p"], other: "and HDTV in a pinch" },
    };
    expect(validate(BOTH, draft)).toBeNull();
  });

  it("refuses an approval with no questions rather than sending an empty answer", () => {
    expect(validate([], {})).toContain("nothing to answer");
  });

  it("answers a question that offers no options at all, through the other box", () => {
    const open: ApprovalQuestion = {
      header: null,
      question: "Which host should it run on?",
      multi_select: false,
      options: [],
    };
    expect(validate([open], emptyDraft([open]))).not.toBeNull();
    expect(validate([open], setOther(emptyDraft([open]), open, "jprod-sb"))).toBeNull();
  });
});

describe("toAnswers", () => {
  it("sends one entry per question, in the order they were asked", () => {
    let draft = toggleOption(emptyDraft(BOTH), HDR, "Only for 4K");
    draft = toggleOption(draft, PROFILES, "Remux");
    draft = toggleOption(draft, PROFILES, "WEBDL-2160p");
    expect(toAnswers(BOTH, draft)).toEqual([
      { question: HDR.question, options: ["Only for 4K"], text: null },
      { question: PROFILES.question, options: ["Remux", "WEBDL-2160p"], text: null },
    ]);
  });

  it("trims free text and sends null rather than an empty string", () => {
    let draft = setOther(emptyDraft(BOTH), HDR, "  a projector, actually  ");
    draft = toggleOption(draft, PROFILES, "Remux");
    expect(toAnswers(BOTH, draft)).toEqual([
      { question: HDR.question, options: [], text: "a projector, actually" },
      { question: PROFILES.question, options: ["Remux"], text: null },
    ]);
  });
});
