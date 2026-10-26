#!/usr/bin/env python3
"""Generate draft candidates from the fine-tuned voice model on Modal.

Stdlib-only client for the vLLM OpenAI-compatible endpoint deployed by
~/Dropbox/Blogg/docs/brand/finetune/modal_voice.py. The endpoint scales to zero,
so the first call after idle can take a few minutes while the container boots —
the request is held, not dropped.

Usage:
    export VOICE_ENDPOINT=https://<workspace>--voice-lora-serve.modal.run
    python3 voice_generate.py --prompt "Write an essay about ..." --n 6 --temperature 0.9
    python3 voice_generate.py --prompt-file brief.md --n 6 --json
    python3 voice_generate.py --prompt "..." --outdir /tmp/candidates

Output: candidates separated by a marker line (default), a JSON list (--json),
or one .md file per candidate (--outdir).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SEPARATOR = "\n===== CANDIDATE {i} =====\n"


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--prompt", help="generation prompt text")
    src.add_argument("--prompt-file", help="read the prompt from this file")
    p.add_argument("--system", default=None, help="optional system message")
    p.add_argument("--n", type=int, default=6, help="number of candidates (default 6)")
    p.add_argument("--temperature", type=float, default=0.9)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--max-tokens", type=int, default=1600)
    p.add_argument("--model", default="voice", help='served model name (default "voice" = LoRA)')
    p.add_argument("--endpoint", default=os.environ.get("VOICE_ENDPOINT"),
                   help="base URL; defaults to $VOICE_ENDPOINT")
    p.add_argument("--timeout", type=int, default=900,
                   help="request timeout in seconds; generous to ride out cold starts")
    p.add_argument("--json", action="store_true", help="print candidates as a JSON list")
    p.add_argument("--outdir", help="write candidate_<i>.md files here instead of stdout")
    return p


def generate(args):
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    prompt = args.prompt if args.prompt is not None else open(args.prompt_file).read()
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({
        "model": args.model,
        "messages": messages,
        "n": args.n,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "max_tokens": args.max_tokens,
    }).encode()

    url = args.endpoint.rstrip("/") + "/v1/chat/completions"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        data = json.load(resp)
    return [c["message"]["content"] for c in data["choices"]]


def main():
    args = build_parser().parse_args()
    if not args.endpoint:
        sys.exit("error: no endpoint — set $VOICE_ENDPOINT or pass --endpoint")

    try:
        candidates = generate(args)
    except urllib.error.HTTPError as e:
        sys.exit(f"error: endpoint returned {e.code}: {e.read().decode(errors='replace')[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach endpoint (cold start can take minutes): {e.reason}")

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        for i, text in enumerate(candidates):
            path = os.path.join(args.outdir, f"candidate_{i}.md")
            with open(path, "w") as f:
                f.write(text)
            print(path)
    elif args.json:
        json.dump(candidates, sys.stdout, indent=2)
        print()
    else:
        for i, text in enumerate(candidates):
            print(SEPARATOR.format(i=i), end="")
            print(text)


if __name__ == "__main__":
    main()
