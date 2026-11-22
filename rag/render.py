import argparse
import json
import os
import sys

import numpy as np
import pedalboard
import soundfile as sf
from scipy.signal import welch

from evaluate import eq_expectations
from suggest_fx import run, validate_chain

REGIONS = [("low_under_250Hz", 20, 250), ("lowmid_250_1000Hz", 250, 1000),
           ("mid_1000_4000Hz", 1000, 4000), ("high_4000_10000Hz", 4000, 10000),
           ("air_over_10000Hz", 10000, 20000)]


def region_levels(audio, rate):
    mono = audio.mean(axis=0)
    freqs, power = welch(mono, rate, nperseg=min(8192, len(mono)))
    levels = {}
    for name, low, high in REGIONS:
        mask = (freqs >= low) & (freqs < min(high, rate / 2))
        if mask.sum() == 0:
            levels[name] = None
            continue
        levels[name] = float(10 * np.log10(power[mask].sum() + 1e-18))
    return levels


def render(audio, rate, chain):
    plugins = []
    for item in chain:
        plugins.append(getattr(pedalboard, item["type"])(**item["params"]))
    out = pedalboard.Pedalboard(plugins)(audio, rate)
    peak = float(np.abs(out).max())
    scaled = False
    if peak > 0.99:
        out = out * (0.99 / peak)
        scaled = True
    return out, scaled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("description", nargs="*")
    ap.add_argument("--chain", help="json file with {\"effects\": [...]} instead of asking the model")
    ap.add_argument("--mode", default="retrieval", choices=["baseline", "none", "retrieval"])
    args = ap.parse_args()

    description = " ".join(args.description)
    directory = os.path.dirname(os.path.abspath(__file__))

    if args.chain:
        with open(args.chain) as f:
            chain, warnings = validate_chain(json.load(f))
    elif description != "":
        chain, warnings, notes, retrieved = run(description, args.mode)
    else:
        print("give a description or --chain")
        sys.exit(1)

    data, rate = sf.read(args.input, dtype="float32", always_2d=True)
    audio = np.ascontiguousarray(data.T)
    out, scaled = render(audio, rate, chain)
    sf.write(args.output, out.T, rate, subtype="PCM_24")

    before = region_levels(audio, rate)
    after = region_levels(out, rate)
    expect = None
    if description != "":
        with open(os.path.join(directory, "corpus", "eq.json")) as f:
            expect = eq_expectations(description, json.load(f)["entries"])

    print()
    print("  chain: " + ", ".join(item["type"] for item in chain))
    if scaled:
        print("  output peaked above 0.99, scaled down to avoid clipping")
    print("  %-22s %8s %8s %8s  %s" % ("region", "before", "after", "change", "expected"))
    changes = {}
    passes = 0
    checks = 0
    for name, low, high in REGIONS:
        if before[name] is None:
            continue
        changes[name] = after[name] - before[name]
        mark = ""
        if expect is not None and name in expect:
            checks = checks + 1
            mark = "up" if expect[name] > 0 else "down"
            if expect[name] * changes[name] >= 0.5:
                passes = passes + 1
                mark = mark + "  ok"
            else:
                mark = mark + "  MISSED"
        print("  %-22s %8.1f %8.1f %+8.1f  %s" % (name, before[name], after[name], changes[name], mark))
    if checks > 0:
        print("  direction: " + str(passes) + " of " + str(checks) + " expected regions moved at least 0.5 dB the right way")
    elif description != "":
        print("  no single eq word in the description, so no direction check")

    with open(args.output + ".json", "w") as f:
        json.dump({"description": description, "input": args.input, "chain": chain,
                   "warnings": warnings, "change_db": changes, "expected": expect}, f, indent=1)
    print("  wrote " + args.output + " and " + args.output + ".json")


if __name__ == "__main__":
    main()