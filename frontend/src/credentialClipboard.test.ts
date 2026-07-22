import { describe, expect, it } from "vitest";
import { CredentialClipboardError, parseClipboardCredential } from "./credentialClipboard";

describe("credential clipboard parser", () => {
  it.each([
    ["sk-example-value", "sk-example-value"],
    ["Bearer sk-example-value", "sk-example-value"],
    ["Authorization: Bearer sk-example-value", "sk-example-value"],
    ['OPENAI_API_KEY="sk-example-value"', "sk-example-value"],
    ["DEEPSEEK_API_KEY='sk-example-value'", "sk-example-value"],
  ])("normalizes a supported single-line form", (input, expected) => {
    expect(parseClipboardCredential(input)).toBe(expected);
  });

  it.each([
    ["sk-one\nsk-two", "CONTROL"],
    ["sk-one,sk-two", "MULTIPLE"],
    ["UNKNOWN_TOKEN=sk-example", "FORMAT"],
    ['OPENAI_API_KEY="sk-example', "FORMAT"],
    ["x".repeat(8193), "TOO_LONG"],
  ])("fails closed for unsafe clipboard content", (input, code) => {
    expect(() => parseClipboardCredential(input)).toThrow(CredentialClipboardError);
    try {
      parseClipboardCredential(input);
    } catch (error) {
      expect((error as CredentialClipboardError).code).toBe(code);
    }
  });
});
