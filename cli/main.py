import os
import shutil
import subprocess
import sys
import click
import soundfile as sf
import math
import time
import json
import datetime
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORD_BIN = os.path.join(ROOT, "build", "record")
PLAY_BIN = os.path.join(ROOT, "build", "play")

IN_PORT_PREFIX = "Studio 68c Pro:capture_AUX"
OUT_PORT_1 = "Studio 68c Pro:playback_AUX0"
OUT_PORT_2 = "Studio 68c Pro:playback_AUX1"
RING_SEC = 2.0
MAX_TRACKS = 16
SESSION_VERSION = 2
DEFAULT_BPM = 120

def session_path(session):
    return session + "/session.json"

def build_session(session, bpm, ports):
    files = track_files(session)
    tracks = []
    rate = None
    for i in range(len(files)):
        info = sf.info(files[i])
        if rate is None:
            rate = info.samplerate
        port = None
        if i < len(ports) and ports[i] != "-":
            port = ports[i]
        tracks.append({
            "id": i + 1,
            "name": "track " + str(i + 1),
            "file": "track_" + str(i + 1) + ".wav",
            "source_port": port,
            "gain": 1.0,
            "mute": False,
            "length_frames": info.frames,
        })
    return {
        "version": SESSION_VERSION,
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sample_rate": rate,
        "bpm": bpm,
        "tracks": tracks,
    }

def save_session(session, data):
    path = session_path(session)
    with open(path + ".temp", "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".temp", path) # write to temp then swap, because early exits were messing this up 

def clear_session(session):
    for path in track_files(session) + glob.glob(session + "/track_*_fx*.wav"):
        os.remove(path)
    if os.path.exists(session_path(session)):
        os.remove(session_path(session))

def log_path(session):
    return session + "/commands.jsonl"

def read_log(session):
    path = log_path(session)
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for number, line in enumerate(f, 1):
            if line.strip() == "":
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                raise click.ClickException(path + " line " + str(number) + " is not valid json")
    return entries

def next_log_number(session):
    entries = read_log(session)
    if len(entries) == 0:
        return 1
    return entries[-1]["n"] + 1

def append_log(session, given):
    entry = {"n": next_log_number(session), "time": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    entry.update(given)
    with open(log_path(session), "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()
        os.fsync(f.fileno())

def replay(entries):
    undo_stack = []
    redo_stack = []
    for entry in entries:
        if entry["cmd"] == "record":
            undo_stack = []
            redo_stack = []
        elif entry["cmd"] == "undo":
            if len(undo_stack) > 0:
                redo_stack.append(undo_stack.pop())
        elif entry["cmd"] == "redo":
            if len(redo_stack) > 0:
                undo_stack.append(redo_stack.pop())
        else:
            undo_stack.append(entry["n"])
            redo_stack = []
    return undo_stack, redo_stack

def apply_changes(data, changes, which):
    for change in changes:
        target = data
        if change["track"] is not None:
            target = None
            for track in data["tracks"]:
                if track["id"] == change["track"]:
                    target = track
            if target is None:
                raise click.ClickException("track " + str(change["track"]) + " no longer exists")
        value = change[which]
        if value is None and change["field"] == "fx":
            target.pop("fx", None)
        else:
            target[change["field"]] = value

def commit(name, data, cmd, args, changes):
    check_session(name, data)
    append_log(name, {"cmd": cmd, "args": args, "changes": changes})
    finish_session(name, data)

def short(value):
    if value is None:
        return "none"
    if isinstance(value, dict):
        return "'" + str(value.get("description", "")) + "'"
    return str(value)

def describe(entry):
    parts = []
    for change in entry.get("changes", []):
        where = "session"
        if change["track"] is not None:
            where = "track " + str(change["track"])
        parts.append(where + " " + change["field"] + " " + short(change["before"]) + " -> " + short(change["after"]))
    return entry["cmd"] + ": " + "; ".join(parts)

def open_session(name):
    if not os.path.isdir(name):
        raise click.ClickException(name + " not found")
    return load_session(name)

#stores data edited from edit session
def finish_session(name, data): 
    check_session(name, data)
    data["modified"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    save_session(name, data)

def get_track(data, selector):
    matches = []
    for track in data["tracks"]:
        if str(track["id"]) == selector or track.get("name") == selector:
            matches.append(track)
    if len(matches) == 0:
        raise click.ClickException("no track '" + selector + "' in this session")
    if len(matches) > 1:
        raise click.ClickException("'" + selector + "' matches more than one track, use the id")
    return matches[0]

def is_number(x):
    return isinstance(x, (int,float)) and not isinstance(x, bool)

def track_files(session):
    files = []
    i = 1
    while os.path.exists(session + "/track_" + str(i) + ".wav"):
        files.append(session + "/track_" + str(i) + ".wav")
        i += 1
    return files

def pick_outputs(output_ports):
    if len(output_ports) > 2:
        click.echo("  give at most 2 output ports")
        sys.exit(1)
    out1 = OUT_PORT_1
    out2 = OUT_PORT_2
    if len(output_ports) == 1:
        out1 = output_ports[0]
        out2 = "-"
    elif len(output_ports) == 2:
        out1 = output_ports[0]
        out2 = output_ports[1]
    return out1, out2

# check to see if session.json is valid, files exist, parameters valid
def check_session(session, data):
    path = session_path(session)
    warnings = []

    def fail(message):
        raise click.ClickException(path + ": " + message)

    if not isinstance(data, dict):
        fail("expected a json object")
    if not isinstance(data.get("version"), int) or data["version"] > SESSION_VERSION:
        fail("unsupported version " + str(data.get("version")))
    if not is_number(data.get("bpm")) or data["bpm"] < 20 or data["bpm"] > 300:
        fail("bpm must be a number between 20 and 300")
    if not isinstance(data.get("tracks"), list):
        fail("tracks must be a list")

    ids = []
    names = []
    for track in data["tracks"]:
        if not isinstance(track, dict):
            fail("each track must be an object")
        if not isinstance(track.get("id"), int) or track["id"] in ids:
            fail("track ids must be unique whole numbers")
        name = "track " + str(track["id"])
        if not is_number(track.get("gain")) or not (0 <= track["gain"] <= 2):
            fail(name + ": gain must be a number from 0 to 2")
        ids.append(track["id"])
        file = track.get("file")
        if not isinstance(file, str) or "/" in file or "\\" in file or ".." in file:
            fail(name + ": file must be a plain file name inside the session folder")
        if not is_number(track.get("gain")) or track["gain"] < 0:
            fail(name + ": gain must be a number, 0 or more")
        if not isinstance(track.get("mute"), bool):
            fail(name + ": mute must be true or false")
        track_name = track.get("name")
        if not isinstance(track_name, str) or track_name.strip() == "" or len(track_name) > 40:
            fail(name + ": name must be 1 to 40 characters")
        if track_name.isdigit():
            fail(name + ": name can't be only digits (those are track ids)")
        if track_name in names:
            fail("track names must be unique, '" + track_name + "' is used twice")
        names.append(track_name)

        fx = track.get("fx")
        if fx is not None:
            fx_file = fx.get("file") if isinstance(fx, dict) else None
            if not isinstance(fx, dict) or not isinstance(fx.get("chain"), list) or not isinstance(fx_file, str) or "/" in fx_file or "\\" in fx_file or ".." in fx_file:
                fail(name + ": fx must be an object with a chain list and a plain file name")
            if not os.path.exists(session + "/" + fx_file):
                warnings.append(name + ": " + fx_file + " is missing, run render again")

        wav = session + "/" + file
        if not os.path.exists(wav):
            warnings.append(name + ": " + file + " is missing")
        elif sf.info(wav).frames != track.get("length_frames"):
            warnings.append(name + ": " + file + " length does not match session.json")
    return warnings

def load_session(session):
    path = session_path(session)
    if not os.path.exists(path):
        return build_session(session, DEFAULT_BPM, [])
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as e:
        raise click.ClickException(path + " is not valid json: " + str(e))
    for warning in check_session(session, data):
        click.echo("  " + paint("session warning: " + warning, AMBER))
    return data

# runs play or record
def run_program(args, process_name):
    if not os.path.exists(args[0]): # missing c functions
        click.echo("missing " + args[0] + " - run make first")
        return 1

    if shutil.which("pw-jack"): # pipewire command
        args = ["pw-jack"] + args

    proc = subprocess.Popen(args, stdout=subprocess.PIPE,stderr=subprocess.STDOUT, text=True) #start proc with given args
    start = time.time()
    presses = 0

    while proc.poll() is None:
        try:
            if sys.stdout.isatty():
                click.echo(status_line(process_name, time.time() - start), nl=False)
            time.sleep(0.1)
        except KeyboardInterrupt:
            # to ignore C process getting interrupted immediately, so recording doesnt error
            presses = presses + 1
            if presses >= 2:
                proc.terminate()

    output = proc.communicate()[0]

    if sys.stdout.isatty():
        click.echo("\r\033[2K", nl=False)    # erase the status line
    for line in output.splitlines():
        click.echo("  " + color_line(line))
    return proc.returncode


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """beluga - multitrack recorder with AI effects selection (not yet implemented)"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help()) # display help if no command given


@cli.command()
@click.argument("name")
@click.option("-s", "--seconds", default=0.0, help="Specify number of seconds you want to record, default = 0.0, means record until interrupt [Ctrl-C]")
@click.option("-t", "--tracks", default=1, help=f'Number of tracks, must be in range of 1 to {MAX_TRACKS}')
@click.option("-i", "--input-port", "input_ports", multiple=True, help='-i PORT1 -i PORT2. overrides --tracks. default is Studio 68c for my own personal testing, will be switched in later deliverable.') # TODO: !!!
@click.option("-b", "--bpm", default=120, help="Adds click-track/metronome to session, default is 120")
@click.option("-o", "--output-port", "output_ports", multiple=True, help='-o LEFT -o RIGHT, where the metronome click is heard. max 2 ports')
@click.option("--met", is_flag=True, help="Play the metronome click at --bpm while recording")
def record(name, seconds, tracks, input_ports, bpm, output_ports, met):
    """Record tracks in session folder name, one wav per track."""
    session = name

    if tracks < 1 or tracks > MAX_TRACKS:
        click.echo(f'  please allocate atleast 1 track and max {MAX_TRACKS}')
        sys.exit(1)

    #if bpm < 20 or bpm > 300:
    #    click.echo(f'  bpm given: {}, must be between 20 and 300')
    #    sys.exit(1)

    if os.path.exists(session):
        if not click.confirm("  " + session + " already exists. Overwrite?"):
            return
        clear_session(session) # TODO: should only override the tracks being rerecorded 


    ports = []
    if len(input_ports) > 0:
        for port in input_ports:
            ports.append(port)
        tracks = len(ports)
    else:
        for i in range(tracks):
            ports.append(IN_PORT_PREFIX + str(i))
    outport1, outport2 = pick_outputs(output_ports)
    bpm_arg = "0"
    if met:
        bpm_arg = str(bpm)
        click.echo("  metronome on at " + bpm_arg + " bpm")
    code = run_program([RECORD_BIN, session, str(seconds), str(RING_SEC), bpm_arg, outport1, outport2] + ports, "recording")
    if code != 0:
        click.echo("  record failed (exit code " + str(code) + ")")
        sys.exit(1)

    click.echo("  saved " + session + '/')

    data = build_session(session, bpm, ports)
    save_session(session, data)
    append_log(session, {"cmd": "record", "args": {"tracks": len(ports), "bpm": bpm}})

    for path in track_files(session):
        draw_waveform(path)

@cli.command()
@click.argument("name")
@click.option("-o", "--output-port", "output_ports", multiple=True, help='-o LEFT -o RIGHT. max 2 ports') # TODO: don't make 68c default, add an intuitive selection / setting menu to later deliverable
@click.option("--met", is_flag=True, help="Play the metronome click at --bpm while playing")
@click.option("--fx", is_flag=True, help="Play the rendered (effects) version of tracks that have one")
def play(name, output_ports, met, fx):
    """Play SESSION_NAME"""
    outport1, outport2 = pick_outputs(output_ports)

    data = load_session(name)
    files = []
    track_args = []
    muted = 0

    for track in data["tracks"]:
        path = name + "/" + track["file"]
        if fx and track.get("fx") is not None and os.path.exists(name + "/" + track["fx"]["file"]):
            path = name + "/" + track["fx"]["file"]
        if not os.path.exists(path):
            continue # load_session already warns
        if track["mute"]:
            muted = muted + 1
            continue
        files.append(path)
        track_args.append("%.4f:%s" % (track["gain"], path))

    if len(files) == 0:
        if muted > 0:
            click.echo("  every track in " + name + " is muted")
        else:
            click.echo("  no tracks found in " + name)
        sys.exit(1)
    if muted > 0:
        click.echo("  " + str(muted) + " track(s) muted, playing " + str(len(files)))
    bpm_arg = "0"
    if met:
        bpm_arg = str(data["bpm"])
        click.echo("  metronome on at " + bpm_arg + " bpm")
    for path in files:
        draw_waveform(path)
    code = run_program([PLAY_BIN, str(RING_SEC), bpm_arg, outport1, outport2] + track_args, "playing")
    if code != 0:
        click.echo("  play failed (exit code " + str(code) + ")")
        sys.exit(1)

@cli.command("rename")
@click.argument("name")
@click.argument("track")
@click.argument("new_name")
def rename_track(name, track, new_name):
    """Rename a track"""
    data = open_session(name)
    track_info = get_track(data, track)
    old_name = track_info["name"]
    track_info["name"] = new_name
    commit(name, data, "rename", [track_info["id"], new_name], [{"track": track_info["id"], "field": "name", "before": old_name, "after": new_name}])
    click.echo("  track " + str(track_info["id"]) + ": name '" + old_name + " to " + new_name + "'")


@cli.command("undo")
@click.argument("name")
def undo_command(name):
    """Undo the last change to the session (rename, mute, gain, bpm, render)."""
    entries = read_log(name)
    undo_stack, redo_stack = replay(entries)
    if len(undo_stack) == 0:
        click.echo("  nothing to undo")
        return
    target = [e for e in entries if e["n"] == undo_stack[-1]][0]
    data = open_session(name)
    apply_changes(data, target["changes"], "before")
    check_session(name, data)
    append_log(name, {"cmd": "undo", "target": target["n"]})
    finish_session(name, data)
    click.echo("  undid #" + str(target["n"]) + " " + describe(target))


@cli.command("redo")
@click.argument("name")
def redo_command(name):
    """Redo the last undone change."""
    entries = read_log(name)
    undo_stack, redo_stack = replay(entries)
    if len(redo_stack) == 0:
        click.echo("  nothing to redo")
        return
    target = [e for e in entries if e["n"] == redo_stack[-1]][0]
    data = open_session(name)
    apply_changes(data, target["changes"], "after")
    check_session(name, data)
    append_log(name, {"cmd": "redo", "target": target["n"]})
    finish_session(name, data)
    click.echo("  redid #" + str(target["n"]) + " " + describe(target))


@cli.command("history")
@click.argument("name")
def history_command(name):
    """Show the command log: what was changed, and what can be undone or redone."""
    if not os.path.isdir(name):
        raise click.ClickException(name + " not found")
    entries = read_log(name)
    undo_stack, redo_stack = replay(entries)
    if len(entries) == 0:
        click.echo("  no commands logged yet")
        return
    for entry in entries:
        if entry["cmd"] in ("undo", "redo"):
            continue
        if entry["cmd"] == "record":
            click.echo("  " + str(entry["n"]).ljust(4) + paint("record (undo barrier)", AMBER))
            continue
        if entry["n"] in undo_stack:
            status = "applied"
        elif entry["n"] in redo_stack:
            status = "undone, can redo"
        else:
            status = "replaced"
        click.echo("  " + str(entry["n"]).ljust(4) + describe(entry) + "   " + paint("[" + status + "]", MIST))


@cli.command("render")
@click.argument("name")
@click.argument("track")
@click.argument("description", nargs=-1, required=True)
@click.option("--mode", default="retrieval", type=click.Choice(["baseline", "none", "retrieval"]), help="how the model sees the SocialFX data, default retrieval")
def render_track(name, track, description, mode):
    """Apply effects described in words to a track, e.g. render take1 2 warm and spacious. The original wav is kept."""
    data = open_session(name)
    track_info = get_track(data, track)
    source = name + "/" + track_info["file"]
    if not os.path.exists(source):
        raise click.ClickException(source + " is missing")
    script = os.path.join(ROOT, "rag", "render.py")
    if not os.path.exists(script):
        raise click.ClickException("missing " + script)

    text = " ".join(description)
    out_file = "track_%d_fx_%d.wav" % (track_info["id"], next_log_number(name))
    out_path = name + "/" + out_file
    proc = subprocess.run([sys.executable, script, source, out_path, text, "--mode", mode], capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        click.echo("  " + paint(line, MIST))
    if proc.returncode != 0:
        for line in proc.stderr.strip().splitlines()[-5:]:
            click.echo("  " + paint(line, CORAL))
        click.echo("  render failed (exit code " + str(proc.returncode) + ")")
        sys.exit(1)

    sidecar = out_path + ".json"
    with open(sidecar) as f:
        result = json.load(f)
    os.remove(sidecar)
    before_fx = track_info.get("fx")
    track_info["fx"] = {
        "description": text,
        "mode": mode,
        "file": out_file,
        "chain": result["chain"],
        "change_db": result["change_db"],
        "rendered": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    commit(name, data, "render", [track_info["id"], text, mode], [{"track": track_info["id"], "field": "fx", "before": before_fx, "after": track_info["fx"]}])
    click.echo("  track " + str(track_info["id"]) + ": rendered to " + out_path + ", hear it with: play " + name + " --fx")


def set_mute(name, selector, value):
    data = open_session(name)
    track_info = get_track(data, selector)
    if track_info["mute"] == value:
        click.echo("  track " + str(track_info["id"]) + " is already " + ("muted" if value else "unmuted"))
        return
    track_info["mute"] = value
    commit(name, data, "mute" if value else "unmute", [track_info["id"]], [{"track": track_info["id"], "field": "mute", "before": not value, "after": value}])
    click.echo("  track " + str(track_info["id"]) + ": " + ("muted" if value else "unmuted"))


@cli.command("mute")
@click.argument("name")
@click.argument("track")
def mute_track(name, track):
    """Mute a track."""
    set_mute(name, track, True)


@cli.command("unmute")
@click.argument("name")
@click.argument("track")
def unmute_track(name, track):
    """Unmute a track."""
    set_mute(name, track, False)


@cli.command("gain")
@click.argument("name")
@click.argument("track")
@click.argument("value", type=float)
def gain_track(name, track, value):
    """Set a track's gain, 0.0 to 2.0 (1.0 = original volume)."""
    if not (0.0 <= value <= 2.0):
        click.echo("  gain must be between 0.0 and 2.0")
        sys.exit(1)
    data = open_session(name)
    track_info = get_track(data, track)
    old_name = track_info["gain"]
    track_info["gain"] = value
    commit(name, data, "gain", [track_info["id"], value], [{"track": track_info["id"], "field": "gain", "before": old_name, "after": value}])
    click.echo("  track " + str(track_info["id"]) + ": gain %.2f to %.2f" % (old_name, value))


@cli.command("bpm")
@click.argument("name")
@click.argument("value", type=int)
def set_bpm(name, value):
    """Set the session tempo, 20 to 300."""
    if value < 20 or value > 300:
        click.echo("  bpm must be between 20 and 300")
        sys.exit(1)
    data = open_session(name)
    old_name = data["bpm"]
    data["bpm"] = value
    commit(name, data, "bpm", [value], [{"track": None, "field": "bpm", "before": old_name, "after": value}])
    click.echo("  bpm " + str(old_name) + " to " + str(value))

def tune_file(path, a4):
    import collections
    import numpy as np
    import tuner

    if not os.path.exists(path):
        raise click.ClickException(path + " not found")
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    tracker = tuner.PitchTracker(rate)
    results = tracker.feed(data.mean(axis=1))
    voiced = [r for r in results if r is not None]

    click.echo()
    click.echo("  " + click.style("≋ " + path, fg=PEARL, bold=True))
    click.echo("  " + paint(tracker.backend + " YIN, " + str(len(results)) + " analysis frames", MIST))
    if len(voiced) == 0:
        click.echo("  " + paint("no pitched sound found", AMBER))
        click.echo()
        return
    median = float(np.median([r[0] for r in voiced]))
    name, octave, cents = tuner.hz_to_note(median, a4)
    click.echo("  median pitch  %.2f Hz  =  %s%d  %+.0f cents" % (median, name, octave, cents))
    click.echo("  pitched in    %d of %d frames (%.0f%%)" % (len(voiced), len(results), 100.0 * len(voiced) / len(results)))
    counts = collections.Counter()
    for hz, confidence in voiced:
        note, note_octave, note_cents = tuner.hz_to_note(hz, a4)
        counts[note + str(note_octave)] += 1
    common = []
    for note, count in counts.most_common(3):
        common.append(note + " " + str(int(100.0 * count / len(voiced))) + "%")
    click.echo("  most common   " + ", ".join(common))
    click.echo()


def tune_live(input_port, a4):
    import queue
    import numpy as np
    try:
        import jack
    except ImportError:
        raise click.ClickException("live mode needs the JACK client library: pip install JACK-Client")
    import tuner

    if shutil.which("pw-jack") and os.environ.get("BELUGA_PWJACK") is None:
        os.environ["BELUGA_PWJACK"] = "1"
        os.execvp("pw-jack", ["pw-jack", sys.executable] + sys.argv)

    try:
        client = jack.Client("beluga-tuner", no_start_server=True)
    except jack.JackError as e:
        raise click.ClickException("could not connect to JACK: " + str(e))

    sources = [p.name for p in client.get_ports(is_output=True, is_audio=True)]
    source = input_port
    if source is None:
        if IN_PORT_PREFIX + "0" in sources:
            source = IN_PORT_PREFIX + "0"
        else:
            captures = [p for p in sources if "capture" in p.lower()]
            if len(captures) == 0:
                raise click.ClickException("no capture ports found, plug in an input and pass one with -i")
            source = captures[0]
    elif source not in sources:
        raise click.ClickException("port '" + source + "' not found. capture ports: " + ", ".join(sources))

    inport = client.inports.register("in")
    blocks = queue.Queue(maxsize=256)

    @client.set_process_callback
    def process(frames):
        try:
            blocks.put_nowait(inport.get_array().copy())
        except queue.Full:
            pass

    tracker = tuner.PitchTracker(client.samplerate)
    recent = []
    click.echo("  listening to " + source + " (" + tracker.backend + " YIN), Ctrl-C to stop")
    click.echo()
    with client:
        client.connect(source, inport)
        try:
            while True:
                try:
                    results = tracker.feed(blocks.get(timeout=0.3))
                except queue.Empty:
                    results = [None]
                if len(results) == 0:
                    continue
                recent = (recent + results)[-5:]
                voiced = [r for r in recent if r is not None]
                if len(voiced) < 3:
                    click.echo("\r\033[2K  " + paint("listening...", MIST), nl=False)
                    continue
                hz = float(np.median([r[0] for r in voiced]))
                name, octave, cents = tuner.hz_to_note(hz, a4)
                if abs(cents) <= 5:
                    color = ICE
                elif abs(cents) <= 25:
                    color = AMBER
                else:
                    color = CORAL
                line = "\r\033[2K  " + click.style((name + str(octave)).ljust(4), fg=color, bold=True)
                line = line + paint("%+4.0f cents  %7.2f Hz  " % (cents, hz), MIST)
                line = line + paint(tuner.cents_bar(cents), color)
                click.echo(line, nl=False)
        except KeyboardInterrupt:
            pass
    click.echo("\r\033[2K", nl=False)


@cli.command("tune")
@click.option("-f", "--file", "path", help="analyze a wav file instead of listening live")
@click.option("-i", "--input-port", default=None, help="JACK port to listen to. default: the 68c's first capture port, else the first capture port found")
@click.option("--a4", default=440.0, help="reference pitch of A4 in Hz, default 440")
def tune(path, input_port, a4):
    """Tuner: the note you are playing and how many cents sharp or flat, using YIN pitch detection."""
    if a4 < 400 or a4 > 480:
        click.echo("  a4 must be between 400 and 480 Hz")
        sys.exit(1)
    if path is not None:
        tune_file(path, a4)
    else:
        tune_live(input_port, a4)


@cli.command()
@click.argument("name")
def show(name):
    """Draw the waveform of NAME.wav."""
    path = name
    if not os.path.exists(path):
        click.echo("  " + path + " not found")
        sys.exit(1)
    draw_waveform(path)

@cli.command()
@click.argument("name")
def info(name):
    """Show the session state: tempo, tracks, gain and mute."""
    if not os.path.isdir(name):
        click.echo("  " + name + " not found")
        sys.exit(1)

    has_file = os.path.exists(session_path(name))
    data = load_session(name)
    tracks = data["tracks"]
    rate = data.get("sample_rate")

    click.echo()
    click.echo("  " + click.style("≋ " + name, fg=PEARL, bold=True))
    line = str(data["bpm"]) + " bpm  ·  " + str(len(tracks)) + " tracks"
    if rate:
        line = line + "  ·  %.1f kHz" % (rate / 1000.0)
    click.echo("  " + paint(line, MIST))
    click.echo("  " + paint("created " + str(data.get("created", "unknown")), MIST))
    if not has_file:
        click.echo("  " + paint("no session.json, showing defaults (older session)", AMBER))
    click.echo()

    # column width for the names
    name_width = 4
    for track in tracks:
        if len(str(track.get("name", "?"))) > name_width:
            name_width = len(str(track.get("name", "?")))

    header = "id".ljust(4) + "name".ljust(name_width + 2) + "gain".ljust(7) + "mute".ljust(6) + "length".ljust(9) + "source"
    click.echo("  " + paint(header, OCEAN))

    for track in tracks:
        if track["mute"]:
            mute_text = "yes"
        else:
            mute_text = "no"

        frames = track.get("length_frames")
        if rate and is_number(frames):
            length_text = "%.1f s" % (frames / rate)
        else:
            length_text = "?"

        source = track.get("source_port")
        if source is None:
            source = "-"

        row = str(track["id"]).ljust(4)
        row = row + str(track.get("name", "?")).ljust(name_width + 2)
        row = row + ("%.2f" % track["gain"]).ljust(7)
        row = row + mute_text.ljust(6)
        row = row + length_text.ljust(9)
        row = row + source

        if track["mute"]:
            click.echo("  " + paint(row, MIST))      # muted tracks are dimmed
        else:
            click.echo("  " + paint(row, PEARL))
    click.echo()

# ===========================================================================
# Generated UI 
# ===========================================================================

# --- colors (red, green, blue) ---------------------------------------------
SEA = (6, 20, 43)          # waveform background, deep water
DEEP = (16, 52, 112)
OCEAN = (30, 110, 200)
SKY = (70, 170, 235)
ICE = (127, 219, 255)
FOAM = (200, 236, 255)
PEARL = (244, 250, 255)    # beluga white
CORAL = (255, 107, 107)    # clipping, errors
AMBER = (255, 200, 87)     # warnings
MIST = (120, 140, 165)     # plain text

# --- beluga banner ---------------------------------------------------------
ART = [
    r"            _.--~~~~--._                      ",
    r"         .-'            '-.                   ",
    r"       .'   o               '.        __      ",
    r"      (        ___.---._      '-.___.-'  `.   ",
    r"       '.__.--'         '-._              )   ",
    r"            '-._            '~--.___.--~'     ",
    r"                '~--.____.--'                 ",
]
ART_COLORS = [PEARL, PEARL, FOAM, FOAM, ICE, SKY, OCEAN]

WORDMARK = [
    "█▄▄ █▀▀ █   █ █ █▀▀ ▄▀█",
    "█▄█ ██▄ █▄▄ █▄█ █▄█ █▀█",
]
TAGLINE = "canary of the sea  ·  multitrack recorder"

# --- animated wave (taller character = higher level) -----------------------
WAVE_CHARS = "▁▂▃▄▅▆▇█"
WAVE_COLORS = [DEEP, DEEP, OCEAN, OCEAN, SKY, SKY, ICE, PEARL]

# --- waveform display ------------------------------------------------------
BLOCKS = " ▁▂▃▄▅▆▇█"                       # BLOCKS[n] is n/8 of a cell filled
HALF = 5                                    # rows above (and below) the center
ROW_COLORS = [PEARL, FOAM, ICE, SKY, OCEAN]  # center row to outermost row


def paint(text, rgb):
    return click.style(text, fg=rgb)


# "mm:ss" like printf("%d:%02d")
def format_time(seconds):
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return "%d:%02d" % (minutes, secs)


# A moving sine-wave made of block characters. Call it again with a larger
# phase to make it move.
def wave_string(phase, length):
    text = ""
    for i in range(length):
        v = math.sin(i * 0.5 - phase) + math.sin(i * 0.19 + phase * 0.6)
        level = int((v + 2) / 4 * 7.99)      # v is -2..2, level is 0..7
        text = text + click.style(WAVE_CHARS[level], fg=WAVE_COLORS[level])
    return text


def draw_banner():
    click.echo()
    for i in range(len(ART)):
        click.echo("  " + paint(ART[i], ART_COLORS[i]))
    click.echo("  " + wave_string(1.3, 46))
    click.echo()
    click.echo("  " + click.style(WORDMARK[0], fg=PEARL, bold=True))
    click.echo("  " + click.style(WORDMARK[1], fg=ICE, bold=True))
    click.echo("  " + paint(TAGLINE, MIST))
    click.echo()


def draw_header(title):
    click.echo()
    click.echo("  " + click.style("≋ beluga ≋", fg=PEARL, bold=True) + "  " + paint(title, FOAM))
    click.echo("  " + wave_string(1.3, 34))
    click.echo()


# The animated line shown while a C program is running.
def status_line(verb, seconds):
    if int(seconds * 2) % 2 == 0:
        dot = "●"
    else:
        dot = "○"
    line = "\r\033[2K  "
    line = line + click.style(dot, fg=CORAL, bold=True) + " "
    line = line + click.style(verb + " " + format_time(seconds), fg=PEARL, bold=True)
    line = line + "  " + wave_string(seconds * 7, 28)
    line = line + "  " + paint("Ctrl-C to stop", MIST)
    return line


# Color one line of output from a C program.
def color_line(line):
    lower = line.lower()
    if "error" in lower:
        return paint(line, CORAL)
    if "warning" in lower:
        return paint(line, AMBER)
    return paint(line, MIST)


# One character of the waveform.
#   eighths: how much of this cell the bar fills (0 to 8)
#   is_top:  True above the center line, False below it
def wave_cell(eighths, is_top, rgb):
    if eighths == 0:
        return click.style(" ", bg=SEA)
    if eighths == 8:
        return click.style("█", fg=rgb, bg=SEA)
    if is_top:
        # bar grows up from the bottom of the cell, so a normal block works
        return click.style(BLOCKS[eighths], fg=rgb, bg=SEA)
    # Below the center the bar hangs down from the TOP of the cell, but there
    # are no "hanging" block characters. So fill the whole cell with the bar
    # color and draw the empty part on top of it in the background color.
    return click.style(BLOCKS[8 - eighths], fg=SEA, bg=rgb)


def draw_waveform(path):
    data, rate = sf.read(path, dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]                    # first channel only for now
    total = len(data)
    if total == 0:
        click.echo("  " + path + " is empty")
        return

    # one column per slice of the file; remember the loudest sample in each
    width = shutil.get_terminal_size().columns - 6
    if width > 120:
        width = 120
    if width > total:
        width = total

    peaks = []
    for col in range(width):
        start = col * total // width
        end = (col + 1) * total // width
        peaks.append(float(abs(data[start:end]).max()))

    loudest = max(peaks)
    clipped = 0
    for col in range(width):
        if peaks[col] >= 0.98:
            clipped = clipped + 1

    # header
    click.echo()
    click.echo("  " + click.style("≋ " + path, fg=PEARL, bold=True))
    info = "%.1f kHz  ·  %s  ·  %d frames" % (rate / 1000.0, format_time(total / rate), total)
    click.echo("  " + paint(info, MIST))
    if loudest > 0:
        peak_text = "peak %.1f dBFS" % (20 * math.log10(loudest))
    else:
        peak_text = "peak -inf dBFS (silence)"
    if clipped > 0:
        click.echo("  " + paint(peak_text, MIST) + "   " + click.style("clipping!", fg=CORAL, bold=True))
    else:
        click.echo("  " + paint(peak_text, MIST))
    click.echo()

    # how many eighths-of-a-cell each column fills, measured from the center
    biggest = HALF * 8
    fill = []
    for col in range(width):
        amount = int(peaks[col] * biggest + 0.5)
        if amount < 1:
            amount = 1                       # keep a thin line for silence
        if amount > biggest:
            amount = biggest
        fill.append(amount)

    # draw top to bottom: HALF rows above the center, then HALF rows below
    for row in range(HALF * 2):
        if row < HALF:
            is_top = True
            dist = HALF - 1 - row            # rows away from the center
        else:
            is_top = False
            dist = row - HALF

        line = click.style(" ", bg=SEA)
        for col in range(width):
            eighths = fill[col] - dist * 8
            if eighths < 0:
                eighths = 0
            if eighths > 8:
                eighths = 8
            if peaks[col] >= 0.98:
                rgb = CORAL                  # clipping
            else:
                rgb = ROW_COLORS[dist]
            line = line + wave_cell(eighths, is_top, rgb)
        line = line + click.style(" ", bg=SEA)
        click.echo("  " + line)

    # time under the waveform: start on the left, length on the right
    left = "0:00"
    right = format_time(total / rate)
    gap = width - len(left) - len(right)
    if gap < 1:
        gap = 1
    click.echo("   " + paint(left + " " * gap + right, OCEAN))
    click.echo()

if __name__ == "__main__":
    cli()