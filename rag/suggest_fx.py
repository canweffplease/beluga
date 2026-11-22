import csv
import json
import math
import re
import os
import sys

import numpy as np

SCHEMA = {
    "HighpassFilter": {
        "what": "removes low frequencies below the cutoff",
        "params": {
            "cutoff_frequency_hz": (20.0, 400.0, 20.0, "Hz, lower = only removes deep rumble"),
        },
    },
    "LowpassFilter": {
        "what": "removes high frequencies above the cutoff, makes the sound darker",
        "params": {
            "cutoff_frequency_hz": (2000.0, 20000.0, 20000.0, "Hz, lower = darker"),
        },
    },
    "LowShelfFilter": {
        "what": "boosts or cuts everything below the cutoff (body, warmth)",
        "params": {
            "cutoff_frequency_hz": (40.0, 1000.0, 200.0, "Hz"),
            "gain_db": (-9.0, 9.0, 0.0, "dB, positive = boost"),
            "q": (0.3, 2.0, 0.7071, "slope, 0.7 is gentle"),
        },
    },
    "HighShelfFilter": {
        "what": "boosts or cuts everything above the cutoff (brightness, air)",
        "params": {
            "cutoff_frequency_hz": (2000.0, 16000.0, 6000.0, "Hz"),
            "gain_db": (-9.0, 9.0, 0.0, "dB, positive = boost"),
            "q": (0.3, 2.0, 0.7071, "slope, 0.7 is gentle"),
        },
    },
    "PeakFilter": {
        "what": "boosts or cuts a band around the center frequency",
        "params": {
            "cutoff_frequency_hz": (40.0, 16000.0, 1000.0, "Hz, center of the band"),
            "gain_db": (-9.0, 9.0, 0.0, "dB, positive = boost"),
            "q": (0.3, 10.0, 0.7071, "width, higher = narrower band"),
        },
    },
    "Compressor": {
        "what": "evens out loudness by turning down loud parts",
        "params": {
            "threshold_db": (-60.0, 0.0, 0.0, "dB, level where compression starts, lower = more compression"),
            "ratio": (1.0, 20.0, 1.0, "1 = no compression, higher = stronger"),
            "attack_ms": (0.1, 100.0, 5.0, "ms, how fast it reacts"),
            "release_ms": (10.0, 1000.0, 100.0, "ms, how fast it lets go"),
        },
    },
    "Distortion": {
        "what": "adds grit and fuzz by clipping the signal",
        "params": {
            "drive_db": (0.0, 40.0, 0.0, "dB, 0 = clean, higher = more fuzz"),
        },
    },
    "Chorus": {
        "what": "thickens and widens the sound with a slow, shimmering detune",
        "params": {
            "rate_hz": (0.1, 5.0, 1.0, "Hz, speed of the shimmer"),
            "depth": (0.0, 1.0, 0.25, "0 to 1, amount of detune"),
            "centre_delay_ms": (3.0, 15.0, 7.0, "ms, base delay"),
            "feedback": (0.0, 0.5, 0.0, "0 to 0.5, adds a flanging edge"),
            "mix": (0.0, 0.5, 0.0, "0 = off, amount of chorus blended in"),
        },
    },
    "Delay": {
        "what": "repeats the sound as echoes",
        "params": {
            "delay_seconds": (0.05, 1.0, 0.25, "seconds between echoes"),
            "feedback": (0.0, 0.7, 0.0, "0 = one echo, higher = more repeats"),
            "mix": (0.0, 0.5, 0.0, "0 = off, amount of echo blended in"),
        },
    },
    "Reverb": {
        "what": "adds room or hall ambience",
        "params": {
            "room_size": (0.0, 1.0, 0.5, "0 small room, 1 huge hall"),
            "damping": (0.0, 1.0, 0.5, "0 bright tail, 1 dark tail"),
            "wet_level": (0.0, 1.0, 0.0, "amount of reverb, 0 = none"),
            "dry_level": (0.0, 1.0, 1.0, "amount of original sound"),
            "width": (0.0, 1.0, 1.0, "stereo width of the reverb"),
        },
    },
}


EFFECT_ORDER = ["HighpassFilter", "LowShelfFilter", "PeakFilter", "HighShelfFilter", "LowpassFilter",
                "Compressor", "Distortion", "Chorus", "Delay", "Reverb"]


def validate_chain(data):
    warnings = []

    if not isinstance(data, dict) or not isinstance(data.get("effects"), list):
        raise ValueError('expected an object like {"effects": [...]}')

    items = data["effects"]
    if len(items) > 8:
        warnings.append("more than 8 effects, keeping the first 8")
        items = items[:8]

    chain = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in SCHEMA:
            warnings.append("dropped unknown effect: " + str(item)[:60])
            continue
        effect_type = item["type"]
        allowed = SCHEMA[effect_type]["params"]

        given = item.get("params", {})
        if not isinstance(given, dict):
            warnings.append(effect_type + ": params is not an object, using defaults")
            given = {}

        for name in given:
            if name not in allowed:
                warnings.append(effect_type + ": dropped unknown parameter " + name)

        params = {}
        for name in allowed:
            low, high, default, _ = allowed[name]
            if name not in given:
                params[name] = default
                continue
            value = given[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                warnings.append(effect_type + "." + name + ": '" + str(value) + "' is not a number, using default " + str(default))
                params[name] = default
                continue
            if value < low or value > high:
                clamped = min(max(value, low), high)
                warnings.append(effect_type + "." + name + ": " + str(value) + " out of range, clamped to " + str(clamped))
                value = clamped
            params[name] = float(value)
        chain.append({"type": effect_type, "params": params})

    if len(chain) == 0:
        raise ValueError("no usable effects in the chain")
    chain.sort(key=lambda item: EFFECT_ORDER.index(item["type"]))
    return chain, warnings


retrieval_store = {}


def get_retrieval(directory):
    if "model" not in retrieval_store:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise SystemExit("retrieval needs: pip install sentence-transformers")
        retrieval_store["model"] = SentenceTransformer("all-MiniLM-L6-v2")
        for effect in ["eq", "reverb", "comp"]:
            path = os.path.join(directory, "corpus", effect + ".json")
            if not os.path.exists(path):
                raise SystemExit("missing " + path + ", run python3 rag/build_corpus.py first")
            with open(path) as f:
                entries = json.load(f)["entries"]
            vectors = retrieval_store["model"].encode([e["word"] for e in entries], normalize_embeddings=True)
            retrieval_store[effect] = (entries, np.array(vectors))
    return retrieval_store


def run(description, mode="baseline"):
    directory = os.path.dirname(os.path.abspath(__file__))

    env_path = os.path.join(os.path.dirname(directory), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line == "" or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() not in os.environ:
                    os.environ[name.strip()] = value.strip().strip('"').strip("'")

    sections = []
    retrieved = {}
    intro = "Crowd-sourced data on what listeners mean by common words (SocialFX):"
    if mode in ("baseline", "retrieval"):
        if mode == "retrieval":
            store = get_retrieval(directory)
            ignore = ["the", "and", "with", "without", "for", "like", "sound", "sounds", "style", "make", "that",
                      "vocals", "vocal", "guitar", "drums", "voice", "track", "mix", "very", "more", "less"]
            words = [w for w in re.findall(r"[a-z]+", description.lower()) if len(w) > 2 and w not in ignore]
            queries = [description.lower()] + words
            query_vectors = np.array(store["model"].encode(queries, normalize_embeddings=True))
            intro = "Crowd-sourced data on what listeners mean by common words (SocialFX), only the entries closest in meaning to the description:"
        for effect in ["eq", "reverb", "comp"]:
            if mode == "baseline":
                path = os.path.join(directory, "corpus", effect + ".json")
                if not os.path.exists(path):
                    raise SystemExit("missing " + path + ", run python3 rag/build_corpus.py first")
                with open(path) as f:
                    chosen = json.load(f)["entries"][:60]
            else:
                entries, vectors = store[effect]
                best_score = {}
                for q in range(len(queries)):
                    similarity = vectors @ query_vectors[q]
                    for i in np.argsort(-similarity)[:(5 if q == 0 else 3)]:
                        best_score[int(i)] = max(best_score.get(int(i), -1.0), float(similarity[i]))
                order = sorted(best_score, key=lambda i: -best_score[i])[:12]
                chosen = [entries[i] for i in order]
                retrieved[effect] = [e["word"] for e in chosen]
            lines = ["[" + effect + " descriptors]"]
            for entry in chosen:
                levels = []
                for name in entry["relative"]:
                    levels.append(name + "=" + entry["relative"][name]["level"])
                lines.append(entry["word"] + " (n=" + str(entry["n_rows"]) + "): " + ", ".join(levels))
            sections.append("\n".join(lines))

    schema_lines = []
    for effect_type in SCHEMA:
        schema_lines.append(effect_type + ": " + SCHEMA[effect_type]["what"])
        for name in SCHEMA[effect_type]["params"]:
            low, high, default, meaning = SCHEMA[effect_type]["params"][name]
            schema_lines.append("    " + name + " (" + str(low) + " to " + str(high) + "): " + meaning)

    system = (
        "You are an audio engineer's assistant. Turn a plain-language description of a sound "
        "into a short chain of audio effects. The description is the sound the user WANTS, so "
        "'muddy' means make it muddy. Use ONLY the effects and parameters in the schema, "
        "and keep every value inside its range. Use only effects the description clearly calls for, "
        "and respect explicit exclusions such as 'no reverb'. Prefer a small chain (2 to 5 effects) "
        "with moderate settings. Give every parameter of every effect you use."
    )
    user = "Description: " + description + "\n\n"
    if len(sections) > 0:
        user = (
            user
            + intro + "\n\n"
            + "\n\n".join(sections) + "\n\n"
            "How to use the descriptor data:\n"
            '- The numbers are relative. "very low" / "very high" mean compared to the other words, not absolute values.\n'
            '- eq: regions of the frequency range. "very high" in low_under_250Hz = boost the lows (LowShelfFilter or PeakFilter),\n'
            '  "very low" in air_over_10000Hz = cut the highs (HighShelfFilter or LowpassFilter). Stay between about -8 and +8 dB.\n'
            "- reverb: higher decay and delay_time = larger room_size. Lower cutoff_freq = darker tail = higher damping.\n"
            "  The dataset has no mix amount, so choose a sensible wet_level (usually 0.1 to 0.35).\n"
            "- comp: lower threshold = more compression, higher ratio = stronger, slower attack keeps the transients.\n"
            "  Use moderate values (ratio 2 to 6), the dataset's absolute numbers are extreme.\n\n"
        )
    user = (
        user
        + "Effects you may use:\n" + "\n".join(schema_lines) + "\n\n"
        'Reply with JSON only, in this form: {"effects": [{"type": "...", "params": {...}}], "notes": "one sentence on why"}. '
        "The order of the effects is fixed automatically, so list them in any order."
    )

    from openai import OpenAI
    response = OpenAI().chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    text = response.choices[0].message.content

    log_path = os.path.join(os.path.dirname(directory), "logs", "api_usage.csv")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    is_new = not os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["model", "mode", "prompt_tokens", "completion_tokens", "description"])
        writer.writerow([
            response.model,
            mode,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            description,
        ])

    data = json.loads(text)
    chain, warnings = validate_chain(data)
    notes = data["notes"] if isinstance(data.get("notes"), str) else ""
    return chain, warnings, notes, retrieved


def main():
    args = sys.argv[1:]
    mode = "baseline"
    if "--no-corpus" in args:
        mode = "none"
        args.remove("--no-corpus")
    if "--retrieval" in args:
        mode = "retrieval"
        args.remove("--retrieval")
    description = " ".join(args)

    try:
        chain, warnings, notes, retrieved = run(description, mode)
    except ValueError as e:
        print("could not use the model's answer: " + str(e))
        sys.exit(1)

    print()
    print("  " + description)
    for item in chain:
        print("  " + item["type"])
        for name in item["params"]:
            print("      %-22s %g" % (name, item["params"][name]))
    if notes != "":
        print("  why: " + notes)
    for effect in retrieved:
        print("  retrieved " + effect + ": " + ", ".join(retrieved[effect]))
    for warning in warnings:
        print("  warning: " + warning)
    print()
    print(json.dumps({"effects": chain}))


if __name__ == "__main__":
    main()