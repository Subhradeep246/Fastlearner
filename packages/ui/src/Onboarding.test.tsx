import { describe, expect, it } from "vitest";
import { setupFromProfile } from "./Onboarding";

describe("learner setup", () => {
  it("prefills a clearly marked editable demo on first run", () => {
    const setup = setupFromProfile({
      user_id: "learner", owner_user_id: "learner", grade_level: 5,
      timezone: "UTC", study_preferences: {},
    });
    expect(setup.name).toBe("Alex (Demo)");
    expect(setup.subjects).toContain("Computer Science");
    expect(setup.doubleClap).toBe(true);
  });

  it("restores saved person-specific context", () => {
    const setup = setupFromProfile({
      user_id: "learner", owner_user_id: "learner", grade_level: 9,
      timezone: "America/New_York",
      study_preferences: {
        name: "Sam", subjects: ["Biology"], learning_style: "Visual analogies",
        voice_enabled: false, double_clap: false,
      },
    });
    expect(setup.name).toBe("Sam");
    expect(setup.gradeLevel).toBe(9);
    expect(setup.subjects).toEqual(["Biology"]);
    expect(setup.voiceEnabled).toBe(false);
  });
});
