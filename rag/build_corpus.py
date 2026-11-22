import ast
import datetime
import json
import os
import numpy as np
import datasets
from datasets import load_dataset

DATASET = "seungheondoh/socialfx-original"
OUT_DIR = "rag/corpus"

# Filters (these also go into the manifest)
EQ_MIN_CONSISTENCY = 0.6
EQ_MIN_ROWS = 5
OTHER_MIN_ROWS = 15            # Reverb and comp
COMP_RELEASE_TO_MS = 1000.0    # Dataset stores the release time in seconds

# EQ bands are grouped into 5 regions so the LLM gets a short summary too
REGIONS = [
    ("low_under_250Hz", 0, 250),
    ("lowmid_250_1000Hz", 250, 1000),
    ("mid_1000_4000Hz", 1000, 4000),
    ("high_4000_10000Hz", 4000, 10000),
    ("air_over_10000Hz", 10000, 1000000),
]


# Extra is a dict-like string, some rows hold a bare nan
def parse_extra(text):
    text = text.replace("nan", "None")
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return {}


# Words listed after 'agreed': what other raters picked for this setting
def get_agreed(extra):
    marker = "'agreed': '"
    start = extra.find(marker)
    if start < 0:
        return []
    start = start + len(marker)
    end = extra.find("'", start)
    words = []
    for w in extra[start:end].split(","):
        w = w.strip().lower()
        if w != "" and w != "none of the above":
            words.append(w)
    return words


def level_name(percentile):
    if percentile < 20:
        return "very low"
    if percentile < 40:
        return "low"
    if percentile < 60:
        return "about average"
    if percentile < 80:
        return "high"
    return "very high"


# Groups: {word: [list of value-lists]}, names: parameter names for those values
# Returns one entry per word that has enough rows
def make_entries(groups, names, min_rows, summarize):
    entries = []
    for word in groups:
        rows = groups[word]
        if len(rows) < min_rows:
            continue
        values = np.array(rows)
        medians = np.median(values, axis=0)
        q1 = np.percentile(values, 25, axis=0)
        q3 = np.percentile(values, 75, axis=0)
        entry = {
            "word": word,
            "n_rows": len(rows),
            "summary": summarize(medians),
            "spread_iqr": {},
        }
        for i in range(len(names)):
            entry["spread_iqr"][names[i]] = round(float(q3[i] - q1[i]), 4)
        entry["bands_median"] = [round(float(m), 4) for m in medians] if len(medians) == 40 else None
        entries.append(entry)

    # Relative position of each word among all kept words, per summary value
    # (the absolute dataset numbers are odd, so the LLM mostly gets "how high compared to other words")
    if len(entries) > 0:
        for name in entries[0]["summary"]:
            all_values = [e["summary"][name] for e in entries]
            for e in entries:
                v = e["summary"][name]
                below = len([x for x in all_values if x < v])
                same = len([x for x in all_values if x == v])
                percentile = 100.0 * (below + 0.5 * same) / len(all_values)
                if "relative" not in e:
                    e["relative"] = {}
                e["relative"][name] = {"percentile": int(round(percentile)), "level": level_name(percentile)}

    entries.sort(key=lambda e: e["n_rows"], reverse=True)
    return entries


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


os.makedirs(OUT_DIR, exist_ok=True)
manifest = {
    "built": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "source_dataset": DATASET,
    "license": "not stated on the dataset card, check before redistributing",
    "library_versions": {"datasets": datasets.__version__, "numpy": np.__version__},
    "effects": {},
    "notes": [],
}

# ---------------------------------------------------------------- eq
data = load_dataset(DATASET, split="eq")
keys = list(data[0]["param_keys"])

# Center frequency of each band from its key, e.g. RSC_120Hz_band -> 120
freqs = []
for key in keys:
    freqs.append(int(key.split("_")[1].replace("Hz", "")))

groups = {}
used = 0
for row in data:
    extra = parse_extra(row["extra"])
    consistency = extra.get("ratings_consistency")
    if extra.get("lang") != "English" or consistency is None or consistency < EQ_MIN_CONSISTENCY:
        continue
    if len(row["param_values"]) != len(keys):
        continue
    word = row["text"].lower().strip()
    groups.setdefault(word, []).append(list(row["param_values"]))
    used = used + 1


def eq_summary(medians):
    summary = {}
    for name, low, high in REGIONS:
        picked = []
        for i in range(len(freqs)):
            if freqs[i] >= low and freqs[i] < high:
                picked.append(medians[i])
        summary[name] = round(float(np.mean(picked)), 4)
    return summary


entries = make_entries(groups, keys, EQ_MIN_ROWS, eq_summary)
save_json(OUT_DIR + "/eq.json", {
    "effect": "eq",
    "description": "40-band EQ curve per descriptor. Positive = boost, negative = cut, scale is relative (units not documented). bands_median lines up with band_hz.",
    "band_hz": freqs,
    "entries": entries,
})
manifest["effects"]["eq"] = {
    "rows_in_split": len(data),
    "rows_used": used,
    "words_kept": len(entries),
    "label_source": "creator's text",
    "filters": "lang == English, ratings_consistency >= " + str(EQ_MIN_CONSISTENCY) + ", words with >= " + str(EQ_MIN_ROWS) + " rows",
}
print("eq     :", used, "rows used,", len(entries), "words")

# ---------------------------------------------------------------- reverb and comp
for split in ["reverb", "comp"]:
    data = load_dataset(DATASET, split=split)
    all_keys = list(data[0]["param_keys"])
    n = len(data[0]["param_values"])
    names = all_keys[:n]                  # reverb has 6 keys but only 5 values (wet_dry is never filled in)

    groups = {}
    used = 0
    for row in data:
        if len(row["param_values"]) != n:
            continue
        values = list(row["param_values"])
        if split == "comp":
            values[names.index("release_ms")] = values[names.index("release_ms")] * COMP_RELEASE_TO_MS
        agreed = get_agreed(row["extra"])
        for word in agreed:
            groups.setdefault(word, []).append(values)
        if len(agreed) > 0:
            used = used + 1

    def summarize(medians):
        summary = {}
        for i in range(len(names)):
            summary[names[i]] = round(float(medians[i]), 4)
        return summary

    entries = make_entries(groups, names, OTHER_MIN_ROWS, summarize)
    save_json(OUT_DIR + "/" + split + ".json", {
        "effect": split,
        "description": "settings that other raters agreed matched each descriptor. summary = median per parameter, relative = where that median sits among all kept words.",
        "params": names,
        "entries": entries,
    })
    manifest["effects"][split] = {
        "rows_in_split": len(data),
        "rows_used": used,
        "words_kept": len(entries),
        "label_source": "words in the 'agreed' list (other raters), a row counts toward every agreed word",
        "filters": "words with >= " + str(OTHER_MIN_ROWS) + " rows",
        "parameters": names,
    }
    print(split.ljust(7), ":", used, "rows used,", len(entries), "words")

manifest["notes"] = [
    "reverb: dataset lists 6 keys but every row has 5 values, the 6th (wet_dry) is missing, so only the first 5 keys are used",
    "comp: release values were multiplied by " + str(COMP_RELEASE_TO_MS) + " (stored in seconds, named release_ms)",
    "eq: units of the band values are not documented, treat them as relative boost/cut",
    "reverb and comp absolute values are extreme, use the 'relative' fields to guide the LLM",
    "parameters are SocialFX's own, they still have to be mapped to Pedalboard (see rag/effect_reference.json)",
]
save_json(OUT_DIR + "/manifest.json", manifest)
print("wrote", OUT_DIR)