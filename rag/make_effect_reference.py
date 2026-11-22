import json
import pedalboard

EFFECTS = [
    "Gain", "Compressor", "Limiter", "NoiseGate",
    "HighpassFilter", "LowpassFilter", "LowShelfFilter", "HighShelfFilter", "PeakFilter",
    "Reverb", "Delay", "Chorus", "Phaser",
    "Distortion", "Clipping", "Bitcrush",
]

reference = {"pedalboard_version": pedalboard.__version__, "effects": {}}

for name in EFFECTS:
    cls = getattr(pedalboard, name)
    effect = cls()
    params = {}
    for attr in dir(effect):
        if attr.startswith("_"):
            continue
        value = getattr(effect, attr)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            params[attr] = value
    reference["effects"][name] = {"params": params, "doc": (cls.__doc__ or "").strip()}

with open("rag/effect_reference.json", "w") as f:
    json.dump(reference, f, indent=2)
print("wrote rag/effect_reference.json")