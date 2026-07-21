from __future__ import annotations

import argparse
import json
import os

from .probe import ProbeConfig, ProbeType, run_openai_probe
from .suite import load_suite_json, run_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe an OpenAI-compatible model endpoint"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default=os.getenv("LEXSOND_KEY"))
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider-id",
        help="Provider adapter identifier, for example openrouter",
    )
    parser.add_argument(
        "--audio-voice",
        help="Provider-declared voice identifier for an audio_speech probe",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--non-streaming", action="store_true")
    parser.add_argument(
        "--probe-type",
        choices=[probe_type.value for probe_type in ProbeType],
        default=ProbeType.CHAT.value,
        help="Endpoint family and modality smoke test",
    )
    parser.add_argument(
        "--suite-json",
        help="Run a bounded ProbeSuite JSON document instead of a single raw request",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.suite_json:
        if args.probe_type != ProbeType.CHAT.value:
            parser.error("--suite-json currently requires --probe-type chat")
        suite = load_suite_json(args.suite_json)
        result = run_suite(
            suite,
            base_url=args.base_url,
            api_key=args.api_key,
            model=args.model,
        )
    else:
        probe_type = ProbeType(args.probe_type)
        result = run_openai_probe(
            ProbeConfig(
                base_url=args.base_url,
                api_key=args.api_key,
                model=args.model,
                timeout_seconds=args.timeout,
                stream=(
                    not args.non_streaming
                    if probe_type in {ProbeType.CHAT, ProbeType.VISION}
                    else False
                ),
                probe_type=probe_type,
                provider_id=args.provider_id,
                audio_voice=args.audio_voice,
            )
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.status.value == "PASS" else 1)


if __name__ == "__main__":
    main()
