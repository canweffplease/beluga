import argparse
import datetime
import json
import os
import random

import numpy as np
import pedalboard

from suggest_fx import run, SCHEMA, validate_chain


def build_prompts(directory):
    prompts = [
        "warm vocals", "soft vocals", "harsh vocals", "calm vocals",
        "loud vocals", "bright vocals", "heavy vocals",
        "warm and spacious vocals", "awfully muddy vocals",
        "shoegaze style guitar", "lo-fi tape sound", "radio voice", "underwater vocals",
        "dreamy vocals", "punchy drums", "vintage telephone", "huge stadium vocals",
        "clean vocals with no reverb", "warm vocals without compression", "bright guitar with no reverb",
    ]
    with open(os.path.join(directory, "corpus", "reverb.json")) as f:
        entries = json.load(f)["entries"]
    beyond = [e["word"] for e in entries[60:]]
    for word in beyond[::max(1, len(beyond) // 10)][:10]:
        prompts.append(word + " vocals")
    return prompts


def random_chain(rng):
    effects = []
    for effect_type in rng.sample(list(SCHEMA), rng.randint(2, 5)):
        params = {}
        for name in SCHEMA[effect_type]["params"]:
            low, high, default, meaning = SCHEMA[effect_type]["params"][name]
            if name == "cutoff_frequency_hz":
                params[name] = float(np.exp(rng.uniform(np.log(low), np.log(high))))
            else:
                params[name] = rng.uniform(low, high)
        effects.append({"type": effect_type, "params": params})
    chain, warnings = validate_chain({"effects": effects})
    return chain, warnings, "", {}


def eq_expectations(prompt, eq_entries):
    words = prompt.lower().replace(",", " ").split()
    found = [e for e in eq_entries if e["word"] in words]
    if len(found) != 1:
        return None
    expect = {}
    for region in found[0]["summary"]:
        value = found[0]["summary"][region]
        if value > 0.3:
            expect[region] = 1
        elif value < -0.3:
            expect[region] = -1
    if len(expect) == 0:
        return None
    return expect


def region_response(chain):
    regions = [("low_under_250Hz", 20, 250), ("lowmid_250_1000Hz", 250, 1000),
               ("mid_1000_4000Hz", 1000, 4000), ("high_4000_10000Hz", 4000, 10000),
               ("air_over_10000Hz", 10000, 20000)]
    filters = ["HighpassFilter", "LowShelfFilter", "PeakFilter", "HighShelfFilter", "LowpassFilter"]
    plugins = []
    for item in chain:
        if item["type"] in filters:
            plugins.append(getattr(pedalboard, item["type"])(**item["params"]))
    impulse = np.zeros((1, 48000), dtype=np.float32)
    impulse[0, 0] = 1.0
    if len(plugins) > 0:
        impulse = pedalboard.Pedalboard(plugins)(impulse, 48000)
    spectrum = 20 * np.log10(np.abs(np.fft.rfft(impulse[0])) + 1e-9)
    freqs = np.fft.rfftfreq(48000, 1.0 / 48000)
    response = {}
    for name, low, high in regions:
        response[name] = float(spectrum[(freqs >= low) & (freqs < high)].mean())
    return response


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="baseline", choices=["baseline", "none", "random", "retrieval"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0, help="seed for --mode random")
    ap.add_argument("--limit", type=int, default=0, help="only the first N prompts, for a smoke test")
    args = ap.parse_args()

    directory = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(directory)
    prompts = build_prompts(directory)
    if args.limit > 0:
        prompts = prompts[:args.limit]
    with open(os.path.join(directory, "corpus", "eq.json")) as f:
        eq_entries = json.load(f)["entries"]

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    base = os.path.join(root, "logs", "eval_" + args.mode + "_" + stamp)

    rng = random.Random(args.seed)
    calls = 0
    valid = 0
    repairs = 0
    region_checks = 0
    region_passes = 0
    prompt_checks = 0
    prompt_passes = 0
    chains = set()
    effect_counts = {}
    stopped = ""

    with open(base + ".jsonl", "w") as out:
        for prompt in prompts:
            expect = eq_expectations(prompt, eq_entries)
            for rep in range(1, args.runs + 1):
                record = {"prompt": prompt, "mode": args.mode, "run": rep}
                calls = calls + 1
                try:
                    if args.mode == "random":
                        chain, warnings, notes, retrieved = random_chain(rng)
                    else:
                        chain, warnings, notes, retrieved = run(prompt, args.mode)
                except ValueError as e:
                    record["valid"] = False
                    record["error"] = str(e)[:200]
                    out.write(json.dumps(record) + "\n")
                    continue
                except Exception as e:
                    stopped = type(e).__name__ + ": " + str(e)[:200]
                    calls = calls - 1
                    break

                valid = valid + 1
                repairs = repairs + len(warnings)
                record["valid"] = True
                record["chain"] = chain
                record["warnings"] = warnings
                record["retrieved"] = retrieved
                chains.add(json.dumps(chain, sort_keys=True))
                for effect_type in set(item["type"] for item in chain):
                    effect_counts[effect_type] = effect_counts.get(effect_type, 0) + 1

                if expect is not None:
                    response = region_response(chain)
                    passed = 0
                    for region in expect:
                        region_checks = region_checks + 1
                        if expect[region] * response[region] >= 0.5:
                            region_passes = region_passes + 1
                            passed = passed + 1
                    prompt_checks = prompt_checks + 1
                    if passed == len(expect):
                        prompt_passes = prompt_passes + 1
                    record["eq_expected"] = expect
                    record["eq_response_db"] = {k: round(v, 2) for k, v in response.items()}
                out.write(json.dumps(record) + "\n")
                out.flush()
            if stopped != "":
                break

    lines = [
        "mode                  : " + args.mode,
        "prompts / runs        : " + str(len(prompts)) + " / " + str(args.runs),
        "calls                 : " + str(calls),
        "valid json + chain    : " + str(valid) + " (" + ("%.0f" % (100.0 * valid / calls) if calls else "0") + "%)",
        "repairs per valid call: " + ("%.2f" % (repairs / valid) if valid else "n/a"),
        "distinct chains       : " + str(len(chains)) + " of " + str(valid) + " valid",
        "eq direction, regions : " + str(region_passes) + " of " + str(region_checks),
        "eq direction, prompts : " + str(prompt_passes) + " of " + str(prompt_checks) + " (every expected region matches)",
        "chains containing     : " + ", ".join(
            k + " " + ("%.0f" % (100.0 * effect_counts[k] / valid) if valid else "0") + "%"
            for k in sorted(effect_counts, key=lambda k: -effect_counts[k])),
    ]
    if stopped != "":
        lines.append("STOPPED EARLY         : " + stopped)
    with open(base + "_summary.txt", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print("raw results in " + base + ".jsonl")


if __name__ == "__main__":
    main()