const KNOWN_ENVIRONMENT_NAMES = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "DEEPSEEK_API_KEY",
  "GOOGLE_API_KEY",
  "GEMINI_API_KEY",
  "MISTRAL_API_KEY",
  "OPENROUTER_API_KEY",
  "SILICONFLOW_API_KEY",
  "DASHSCOPE_API_KEY",
  "MOONSHOT_API_KEY",
  "GROQ_API_KEY",
  "XAI_API_KEY",
  "NVIDIA_API_KEY",
  "CEREBRAS_API_KEY",
  "PERPLEXITY_API_KEY",
]);

export class CredentialClipboardError extends Error {
  constructor(readonly code: "EMPTY" | "TOO_LONG" | "CONTROL" | "MULTIPLE" | "FORMAT", message: string) {
    super(message);
    this.name = "CredentialClipboardError";
  }
}

export function parseClipboardCredential(input: string): string {
  if (typeof input !== "string" || input.length === 0) {
    throw new CredentialClipboardError("EMPTY", "剪贴板中没有可识别的 API Key");
  }
  if (input.length > 8192) {
    throw new CredentialClipboardError("TOO_LONG", "剪贴板内容超过 8192 字符，已拒绝读取");
  }
  if ([...input].some((character) => character.charCodeAt(0) < 0x20 || character.charCodeAt(0) === 0x7f)) {
    throw new CredentialClipboardError("CONTROL", "API Key 不能包含换行或控制字符");
  }

  let value = input.trim();
  const authorization = /^Authorization\s*:\s*Bearer\s+(.+)$/i.exec(value);
  if (authorization) {
    value = authorization[1];
  } else {
    const bearer = /^Bearer\s+(.+)$/i.exec(value);
    if (bearer) value = bearer[1];
    else {
      const environment = /^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$/.exec(value);
      if (environment) {
        if (!KNOWN_ENVIRONMENT_NAMES.has(environment[1])) {
          throw new CredentialClipboardError("FORMAT", "只接受已知的 API Key 环境变量名称");
        }
        value = environment[2];
      }
    }
  }

  value = stripPairedQuotes(value.trim());
  if (!value) {
    throw new CredentialClipboardError("EMPTY", "剪贴板中没有可识别的 API Key");
  }
  if (/\s/.test(value) || value.includes(",") || value.includes(";")) {
    throw new CredentialClipboardError("MULTIPLE", "检测到多个值或分隔符，请一次只粘贴一个 API Key");
  }
  if (value.length > 8192 || [...value].some((character) => {
    const code = character.charCodeAt(0);
    return code < 0x21 || code > 0x7e;
  })) {
    throw new CredentialClipboardError("FORMAT", "API Key 必须是可见 ASCII 字符且不含空格");
  }
  return value;
}

function stripPairedQuotes(value: string): string {
  if (value.length >= 2) {
    const first = value[0];
    const last = value[value.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return value.slice(1, -1).trim();
    }
    if (first === '"' || first === "'" || last === '"' || last === "'") {
      throw new CredentialClipboardError("FORMAT", "API Key 的引号必须成对出现");
    }
  }
  return value;
}
