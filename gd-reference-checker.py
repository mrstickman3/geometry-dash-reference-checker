#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import Counter, deque
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache

TERMS = []


def add(weight, *specs):
    for spec in specs:
        names = [n.strip() for n in spec.split("|") if n.strip()]
        if names:
            TERMS.append((names[0], weight, names))



add(3,
    "tidal wave|tidal waves",
    "black blizzard",
    "amethyst|ametyst",
    "orbit|orbits",
    "geometry dash|geometry das|geometry dashed",
    "robtop|rob top|rob topala|robtop games",
    "stereo madness",
    "base after base",
    "clutterfunk|clutter funk",
    "theory of everything|theory of everything 2",
    "electroman adventures|electro man adventures",
    "electrodynamix|electro dynamix",
    "hexagon force",
    "blast processing",
    "geometrical dominator",
    "fingerdash|finger dash",
    "sonic wave",
    "nine circles|9 circles",
    "kenos|keynos|kee nos",
    "acheron|aqueron|ache ron|akeron",
    "avernus",
    "clubstep|club step",
    "silent clubstep|silent club step",
    "xstep|x step|ex step",
    "yatagarasu",
    "polargeist|polar geist",
    "forever bound",
    "demon list|the demon list",
    "extreme demon|extreme demons",
    "insane demon|insane demons",
    "mega hack|megahack",
    "gdps|g d p s",
    "pointercrate|pointer crate",
    "gdbrowser|gd browser",
    "viprin|vip rin",
    "npesta|n pesta|en pesta",
    "waterflame|water flame",
    "dorabae|dora bae",
    "onilink|oni link",
    "flamewall|flame wall|fire wall|firewall",
    )


add(2,
    "bloodbath|blood bath",
    "deadlocked|dead locked",
    "cataclysm",
    "slaughterhouse|slaughter house",
    "tartarus",
    "zodiac",
    "thinking space",
    "time machine",
    "back on track",
    "dry out",
    "cant let go|cannot let go|can not let go",
    "jumper",
    "mana orb|mana orbs",
    "jump orb|jump orbs",
    "dash orb|dash orbs",
    "gravity portal",
    "practice mode",
    "icon kit",
    "user coin|user coins",
    "secret coin|secret coins",
    "geode",
    "hard demon",
    "easy demon",
    "medium demon",
    "weekly demon",
    "creator points",
    )


add(1,
    "wave|waves",
    "cube",
    "ship",
    "ufo",
    "ball",
    "robot",
    "spider",
    "swing|swing copter",
    "jetpack",
    "orb|orbs",
    "portal|portals",
    "spike|spikes",
    "platformer",
    "gauntlet",
    "gd",
    "demon|demons",
    "nerf|nerfed",
    "verify|verified",
    )

PROMPT = (
    "Geometry Dash. Levels: Tidal Wave, Black Blizzard, Amethyst, Bloodbath, "
    "Acheron, Kenos, Clubstep, Stereo Madness, Polargeist, Flamewall. Orbit, wave, RobTop."
)



def normalize(text):
    text = text.lower().replace("'", "").replace("\u2019", "")
    return re.sub(r"[^a-z0-9]+", " ", text).split()


PROMPT_TOKENS = set(normalize(PROMPT))


LEVELS = {" ".join(normalize(n)) for n in (
    "tidal wave", "black blizzard", "amethyst", "orbit", "stereo madness",
    "base after base", "clutterfunk", "theory of everything",
    "electroman adventures", "electrodynamix", "hexagon force",
    "blast processing", "geometrical dominator", "fingerdash", "sonic wave",
    "nine circles", "kenos", "acheron", "avernus", "clubstep",
    "silent clubstep", "xstep", "yatagarasu", "polargeist", "bloodbath",
    "deadlocked", "cataclysm", "slaughterhouse", "tartarus", "zodiac",
    "thinking space", "time machine", "back on track", "dry out",
    "cant let go", "jumper", "flamewall",
)}

# Search text used instead of "<level> geometry dash song" (add more with --songs)
DEFAULT_QUERIES = {
    "cant let go": "Can't Let Go Geometry Dash song",
    "geometry dash": "Geometry Dash level songs",
}

# Clip length in seconds for specific terms (everything else uses --play-seconds)
DEFAULT_SECONDS = {
    "tidal wave": 10.0,    # give tidal wave more time since the beginning is really long


def is_level(canon):
    return " ".join(normalize(canon)) in LEVELS
INDEX = []  # (canon, weight, token_count, squashed)


def build_index():
    INDEX.clear()
    for canon, weight, names in TERMS:
        for name in names:
            toks = normalize(name)
            if toks:
                INDEX.append((canon, weight, len(toks), "".join(toks)))
    # longest first so "tidal wave" claims its words before "wave" can
    INDEX.sort(key=lambda e: (-len(e[3]), -e[1]))


@lru_cache(maxsize=20000)
def _close(a, b):
    if abs(len(a) - len(b)) > 2:
        return False
    sm = SequenceMatcher(None, a, b)
    return sm.quick_ratio() >= 0.86 and sm.ratio() >= 0.86


def find_hits(tokens):
    """Return [(start_index, canon, weight)] for every non-overlapping match."""
    used = [False] * len(tokens)
    hits = []
    for canon, weight, n_tok, squashed in INDEX:
        fuzzy_ok = len(squashed) >= 8
        for n in range(max(1, n_tok - 1), n_tok + 2):
            for i in range(len(tokens) - n + 1):
                if any(used[i:i + n]):
                    continue
                cand = "".join(tokens[i:i + n])
                if cand == squashed or (fuzzy_ok and _close(cand, squashed)):
                    hits.append((i, canon, weight))
                    for k in range(i, i + n):
                        used[k] = True
    hits.sort()
    return hits


# --------------------------------------------------------------------------
# Music player
# --------------------------------------------------------------------------
class Player:
    """Finds a level's song on YouTube (free, no login, via yt-dlp) and plays
    the audio stream with mpv or ffplay. Runs in background threads so the
    listener never blocks. If yt-dlp or a player is missing, it falls back to
    opening the YouTube search in your browser."""

    def __init__(self, seconds, volume, mute, overrides, no_proxy, cooldown=20.0,
                 levels_only=False, durations=None):
        self.seconds = seconds      # 0 = play the whole track
        self.volume = volume
        self.mute = mute            # ignore the mic while music is playing
        self.overrides = overrides
        self.no_proxy = no_proxy
        self.cooldown = cooldown    # don't replay the same level this soon
        self.levels_only = levels_only  # False = any detected term plays music
        self.durations = durations or {}  # per-term clip length overrides
        self.backend = shutil.which("mpv") or shutil.which("ffplay")
        self.lock = threading.Lock()
        self.generation = 0
        self.active = False         # True from request until playback ends
        self.started = 0.0
        self.proc = None
        self.last = {}

    def muting(self):
        if not (self.mute and self.active):
            return False
        # safety net: never keep the mic paused longer than a song could last
        if time.monotonic() - self.started > (self.seconds or 900) + 45:
            self.active = False
            return False
        return True

    def play(self, canon):
        key = " ".join(normalize(canon))
        now = time.monotonic()
        with self.lock:
            if self.active and now - self.started < 6:
                return  # one sentence naming two levels: the first one wins
            if now - self.last.get(key, -1e9) < self.cooldown:
                return
            self.last[key] = now
            self.generation += 1
            gen = self.generation
            self.active = True
            self.started = now
        threading.Thread(target=self._run, args=(canon, key, gen), daemon=True).start()

    def stop(self):
        with self.lock:
            was_active = self.active
            self.generation += 1
            self.active = False
        self._kill()
        return was_active

    def _kill(self):
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()

    def _resolve(self, query):
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "bestaudio/best"}
        if self.no_proxy:
            opts["proxy"] = ""
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entry = (info.get("entries") or [info])[0]
        agent = (entry.get("http_headers") or {}).get("User-Agent")
        return (entry["url"], entry.get("title") or query, agent,
                entry.get("webpage_url"))

    def _command(self, url, agent, seconds=0):
        vol = str(self.volume)
        length = f"{seconds:g}" if seconds else None  # counts audio time, not startup
        if os.path.basename(self.backend).startswith("mpv"):
            cmd = [self.backend, "--no-video", "--really-quiet",
                   "--no-terminal", f"--volume={vol}"]
            if agent:
                cmd.append(f"--user-agent={agent}")
            if length:
                cmd.append(f"--length={length}")
            return cmd + [url]
        cmd = [self.backend, "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-volume", vol]
        if agent:
            cmd += ["-user_agent", agent]
        if length:
            cmd += ["-t", length]
        return cmd + [url]

    def _finish(self, gen):
        with self.lock:
            if gen != self.generation or not self.active:
                return
            self.active = False
        if self.mute:
            print("\033[2m♪ finished, listening again\033[0m")

    def _fallback(self, query, why, gen):
        self._finish(gen)
        print(f"\033[1;31m♪ Couldn't play audio ({type(why).__name__}: {why})\033[0m")
        print("   Opening the YouTube search in your browser instead.")
        print("   For autoplay: pip install yt-dlp, install mpv (brew install mpv), "
              "and try --no-proxy if it's a network error.")
        webbrowser.open("https://www.youtube.com/results?search_query="
                        + urllib.parse.quote_plus(query))

    def _run(self, canon, key, gen):
        try:
            self._run_inner(canon, key, gen)
        except Exception as exc:
            print(f"\033[1;31m♪ player error: {type(exc).__name__}: {exc}\033[0m")
        finally:
            self._finish(gen)  # always release the mic, whatever went wrong

    def _run_inner(self, canon, key, gen):
        query = self.overrides.get(key) or f"{canon} geometry dash song"
        seconds = self.durations.get(key, self.seconds)
        self._kill()
        try:
            url, title, agent, page = self._resolve(query)
        except Exception as exc:  # yt-dlp missing, offline, no results...
            self._fallback(query, exc, gen)
            return
        with self.lock:
            if gen != self.generation:
                return  # stopped or replaced while we were searching
        if not self.backend:
            self._finish(gen)
            print(f"\033[1;36m♪ {title}\033[0m  (no mpv/ffplay found, opening browser)")
            webbrowser.open(page or url)
            return
        try:
            proc = subprocess.Popen(self._command(url, agent, seconds),
                                    stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._fallback(query, exc, gen)
            return
        self.proc = proc
        note = ""
        if self.mute:
            note = "  (mic paused until it ends; --no-mute if you use headphones)"
        print(f"\033[1;36m♪ Playing: {title}\033[0m{note}")
        try:
            # the player stops itself after `seconds` of audio; this is a safety net
            proc.wait(timeout=(seconds + 20) if seconds else None)
        except subprocess.TimeoutExpired:
            proc.terminate()
        finally:
            self._finish(gen)


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------
class Detector:
    def __init__(self, threshold, window, log_path, bell, player=None, verbose=True):
        self.threshold = threshold
        self.player = player
        self.verbose = verbose
        self.window = window
        self.log_path = log_path
        self.bell = bell
        self.lock = threading.Lock()
        self.reported = Counter()  # canon -> occurrences handled this utterance
        self.pending = deque()     # (time, canon, weight) low-weight hits

    def _echo_ok(self, text):
        return self.verbose and text and not (self.player and self.player.muting())

    def echo(self, text):
        """Live partial transcript, drawn on one updating gray line."""
        if not self._echo_ok(text):
            return
        width = shutil.get_terminal_size((100, 20)).columns - 6
        line = text.strip()
        if len(line) > width:
            line = "…" + line[-(width - 1):]
        sys.stdout.write("\r\033[2K\033[2m… " + line + "\033[0m")
        sys.stdout.flush()

    def echo_final(self, text):
        """Finished sentence, kept on screen so you can see what it heard."""
        if not self._echo_ok(text):
            return
        sys.stdout.write("\r\033[2K\033[2m  heard: " + text.strip() + "\033[0m\n")
        sys.stdout.flush()

    def reset_utterance(self):
        with self.lock:
            self.reported.clear()

    def feed(self, text, final=False, provisional=False):
        # Music coming out of the speakers would be picked up by the mic and
        # could re-trigger itself, so ignore the mic while a track is playing.
        if self.player and self.player.muting():
            if final:
                self.reset_utterance()
            return
        tokens = normalize(text)
        if self.player and re.search(r"\bstop (the )?music\b", " ".join(tokens)):
            if self.player.stop():
                print("\033[1;33m♪ stopped\033[0m")
                if final:
                    self.reset_utterance()
                return
        if not tokens:
            if final:
                self.reset_utterance()
            return
        # Whisper sometimes parrots its initial prompt back on silence/noise
        if len(tokens) >= 4 and all(t in PROMPT_TOKENS for t in tokens):
            if final:
                self.reset_utterance()
            return

        with self.lock:
            seen = Counter()
            fresh = []
            for _, canon, weight in find_hits(tokens):
                # partial text flickers, so only trust strong terms from it
                if provisional and weight < self.threshold:
                    continue
                seen[canon] += 1
                if seen[canon] > self.reported[canon]:
                    fresh.append((canon, weight))
            for canon, count in seen.items():
                self.reported[canon] = max(self.reported[canon], count)
            for canon, weight in fresh:
                self._score(canon, weight, text)
            if final:
                self.reported.clear()

    def _score(self, canon, weight, text):
        now = time.monotonic()
        while self.pending and now - self.pending[0][0] > self.window:
            self.pending.popleft()

        if weight >= self.threshold:
            self._alert([canon], f"direct, weight {weight}", text)
            return

        self.pending.append((now, canon, weight))
        best = {}
        for _, c, w in self.pending:
            best[c] = max(best.get(c, 0), w)
        if sum(best.values()) >= self.threshold:
            self._alert(sorted(best), "combo: " + " + ".join(
                f"{c}({w})" for c, w in sorted(best.items())), text)
            self.pending.clear()

    def _alert(self, canons, reason, text):
        sys.stdout.write("\r\033[2K")  # clear the live-transcript line
        stamp = datetime.now().strftime("%H:%M:%S")
        label = ", ".join(c.upper() for c in canons)
        heard = text if len(text) <= 120 else text[:117] + "..."
        print(f"\033[1;32m[{stamp}] GD reference: {label}\033[0m  ({reason})")
        print(f"           heard: \"{heard}\"")
        if self.bell:
            sys.stdout.write("\a")
        sys.stdout.flush()
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "terms": canons,
                    "reason": reason,
                    "text": text,
                }) + "\n")
        if self.player:
            pick = next((c for c in canons if is_level(c)), None)
            if pick is None and not self.player.levels_only:
                pick = canons[0]  # wildcard: any detected reference plays music
            if pick:
                self.player.play(pick)


# --------------------------------------------------------------------------
# Setup helpers
# --------------------------------------------------------------------------
def load_extra_terms(path):
    """One term per line. Optional weight prefix and aliases: 2:my term|alias"""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            weight = 3
            m = re.match(r"^([123]):(.*)$", line)
            if m:
                weight, line = int(m.group(1)), m.group(2)
            add(weight, line)


def find_dead_proxy():
    """Return 'host:port' of a configured proxy that refuses connections, else None."""
    import socket
    import urllib.request
    for key, url in urllib.request.getproxies().items():
        if key == "no" or not url:
            continue
        parsed = urllib.parse.urlparse(url if "://" in url else "http://" + url)
        if not parsed.hostname:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            socket.create_connection((parsed.hostname, port), timeout=1).close()
        except OSError:
            return f"{parsed.hostname}:{port}"
    return None


def load_song_overrides(path):
    """One per line: level name = exact YouTube search text"""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, query = line.split("=", 1)
            out[" ".join(normalize(name))] = query.strip()
    return out


def print_mic(device):
    """Say which microphone will be used (the system default unless --device)."""
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        info = (pa.get_device_info_by_index(device) if device is not None
                else pa.get_default_input_device_info())
        pa.terminate()
        print(f"Microphone: [{info['index']}] {info['name']}  "
              "(change with --device N, list with --list-devices)")
    except Exception as exc:
        print(f"Microphone: couldn't query input devices ({type(exc).__name__}: {exc})")


def test_mic(device):
    """Record 5 seconds and show the input level, to tell 'silent' from 'working'."""
    import array
    try:
        import pyaudio
    except ImportError:
        print("PyAudio isn't installed (pip install pyaudio).")
        return
    pa = pyaudio.PyAudio()
    try:
        info = (pa.get_device_info_by_index(device) if device is not None
                else pa.get_default_input_device_info())
    except OSError:
        print("No input device found. Plug in or enable a microphone.")
        pa.terminate()
        return
    rate = int(info.get("defaultSampleRate", 16000))
    print(f"Testing [{info['index']}] {info['name']} at {rate} Hz. Say something for 5 seconds...")
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                     input_device_index=int(info["index"]), frames_per_buffer=1024)
    loudest = 0
    end = time.time() + 5
    while time.time() < end:
        samples = array.array("h", stream.read(1024, exception_on_overflow=False))
        peak = max((abs(s) for s in samples), default=0)
        loudest = max(loudest, peak)
        print(f"\r{'#' * min(40, peak // 400):<40} {peak:6d}", end="", flush=True)
    stream.stop_stream()
    stream.close()
    pa.terminate()
    print()
    if loudest < 200:
        print("Basically silence. Either macOS isn't giving this terminal app microphone "
              "access (System Settings > Privacy & Security > Microphone), or the wrong "
              "input is selected (System Settings > Sound > Input, or use --device).")
    else:
        print(f"The mic is working (loudest level {loudest}). If it still misses your "
              "words, they're reaching Whisper but being misheard: watch the gray text.")


def list_devices():
    import pyaudio
    pa = pyaudio.PyAudio()
    print("Input devices:")
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            print(f"  [{i}] {info['name']}")
    pa.terminate()


def build_recorder(args, detector):
    from RealtimeSTT import AudioToTextRecorder

    kwargs = dict(
        model=args.model,
        language="en",
        realtime_model_type=args.realtime_model,
        enable_realtime_transcription=True,
        realtime_processing_pause=0.2,
        on_realtime_transcription_update=lambda t: (detector.echo(t),
                                                    detector.feed(t, provisional=True)),
        on_realtime_transcription_stabilized=lambda t: detector.feed(t),
        post_speech_silence_duration=0.6,
        spinner=False,
    )
    if args.device is not None:
        kwargs["input_device_index"] = args.device
    if not args.no_prompt:
        kwargs["initial_prompt"] = PROMPT
        kwargs["initial_prompt_realtime"] = PROMPT

    try:
        return AudioToTextRecorder(**kwargs)
    except TypeError:
        # older RealtimeSTT without initial_prompt support
        kwargs.pop("initial_prompt", None)
        kwargs.pop("initial_prompt_realtime", None)
        print("Note: this RealtimeSTT version has no initial_prompt; "
              "running without vocabulary biasing (pip install -U RealtimeSTT).")
        return AudioToTextRecorder(**kwargs)


def parse_args():
    p = argparse.ArgumentParser(description="Detect Geometry Dash references from your mic.")
    p.add_argument("--model", default="base.en", help="final transcription model (default: base.en)")
    p.add_argument("--realtime-model", default="tiny.en", help="fast partial-text model (default: tiny.en)")
    p.add_argument("--device", type=int, default=None, help="microphone device index")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--threshold", type=int, default=3, help="score needed to fire (default: 3)")
    p.add_argument("--window", type=float, default=12.0, help="seconds that low-weight words can combine (default: 12)")
    p.add_argument("--words", help="text file of extra terms")
    p.add_argument("--log", help="append detections to this JSONL file")
    p.add_argument("--no-bell", action="store_true", help="disable the terminal bell")
    p.add_argument("--no-prompt", action="store_true", help="do not bias Whisper with GD vocabulary")
    p.add_argument("--no-play", action="store_true", help="only detect, don't play music")
    p.add_argument("--play-seconds", type=float, default=5.0,
                   help="seconds of each song to play, 0 = whole song (default: 5; "
                        "per-term overrides live in DEFAULT_SECONDS)")
    p.add_argument("--volume", type=int, default=70, help="playback volume 0-100 (default: 70)")
    p.add_argument("--no-mute", action="store_true",
                   help="keep listening while music plays (use headphones); a new level "
                        "switches songs and saying 'stop music' stops it")
    p.add_argument("--songs", help="text file of 'level name = youtube search text' lines")
    p.add_argument("--no-proxy", action="store_true",
                   help="ignore system/env proxies (fixes 'Connection refused' from a dead proxy)")
    p.add_argument("--levels-only", action="store_true",
                   help="only play music for actual level names (default: any detected term plays)")
    p.add_argument("--quiet", action="store_true", help="don't show the live transcript")
    p.add_argument("--test-mic", action="store_true",
                   help="record 5 seconds and show the mic level, then exit")
    return p.parse_args()


def main():
    args = parse_args()
    if args.list_devices:
        list_devices()
        return
    if args.test_mic:
        test_mic(args.device)
        return

    if args.words:
        load_extra_terms(args.words)
    build_index()

    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console

    if not args.no_proxy:
        dead = find_dead_proxy()
        if dead:
            print(f"Note: the configured proxy {dead} isn't accepting connections, "
                  "so proxies are being ignored (same as --no-proxy).")
            args.no_proxy = True

    if args.no_proxy:
        # Setting NO_PROXY also stops Python from reading macOS system proxy
        # settings; RealtimeSTT's worker processes inherit this environment.
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(var, None)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

    player = None
    if not args.no_play:
        overrides = dict(DEFAULT_QUERIES)
        if args.songs:
            overrides.update(load_song_overrides(args.songs))
        player = Player(args.play_seconds, args.volume, not args.no_mute,
                        overrides, args.no_proxy, levels_only=args.levels_only,
                        durations=DEFAULT_SECONDS)
        if not player.backend:
            print("Note: mpv/ffplay not found, so songs will open in your browser "
                  "(brew install mpv for autoplay).")

    detector = Detector(args.threshold, args.window, args.log, not args.no_bell, player,
                        verbose=not args.quiet)
    print_mic(args.device)
    recorder = build_recorder(args, detector)

    print(f"Listening ({len(TERMS)} terms loaded). Say something Geometry Dash. Ctrl+C to quit.")
    print("Gray text is what it hears live. Nothing at all when you talk? "
          "Run: python3 gdr.py --test-mic")
    try:
        while True:
            text = recorder.text()  # blocks until an utterance finishes
            detector.echo_final(text)
            detector.feed(text or "", final=True)
    except KeyboardInterrupt:
        pass
    finally:
        if player:
            player.stop()
        recorder.shutdown()


# Required: RealtimeSTT spawns worker processes (mandatory on Windows/macOS)
if __name__ == "__main__":
    main()
