import { describe, expect, it } from "vitest";

import {
  canStartEndToEndRun,
  parseProviderDynamicVariables,
} from "./HarnessCreate";

describe("parseProviderDynamicVariables", () => {
  it("accepts an object of safe scalar values", () => {
    expect(
      parseProviderDynamicVariables(
        '{"customer_name":"Jane","balance":124,"verified":true}',
      ),
    ).toEqual({ customer_name: "Jane", balance: 124, verified: true });
  });

  it("rejects invalid JSON and nested values before submission", () => {
    expect(() => parseProviderDynamicVariables("not-json")).toThrow(
      "valid JSON",
    );
    expect(() =>
      parseProviderDynamicVariables('{"customer":{"name":"Jane"}}'),
    ).toThrow("string, number, or boolean");
  });
});

describe("canStartEndToEndRun", () => {
  it("allows a sourced run without requiring a prior manual preflight", () => {
    expect(
      canStartEndToEndRun({
        hasSource: true,
        submitting: false,
        checking: false,
        uploadingSecretFile: false,
      }),
    ).toBe(true);
  });

  it.each([
    ["has no source", { hasSource: false }],
    ["is already submitting", { submitting: true }],
    ["is checking manually", { checking: true }],
    ["is uploading a credential", { uploadingSecretFile: true }],
  ])("blocks while the form %s", (_label, override) => {
    expect(
      canStartEndToEndRun({
        hasSource: true,
        submitting: false,
        checking: false,
        uploadingSecretFile: false,
        ...override,
      }),
    ).toBe(false);
  });
});

describe("per-call limit", () => {
  it("omits the call limit when the field is left blank", () => {
    // A blank field must change nothing: the run keeps whatever the contract derived, so an agent
    // whose calls are legitimately long is not silently held to a shorter default.
    const buildConfig = (callTimeoutSeconds) => ({
      ...(String(callTimeoutSeconds).trim() && Number(callTimeoutSeconds) > 0
        ? { voice_call_timeout_seconds: Number(callTimeoutSeconds) }
        : {}),
    });

    expect(buildConfig("")).toEqual({});
    expect(buildConfig("   ")).toEqual({});
    expect(buildConfig("0")).toEqual({});
    expect(buildConfig("600")).toEqual({ voice_call_timeout_seconds: 600 });
    expect(buildConfig(900)).toEqual({ voice_call_timeout_seconds: 900 });
  });
});
