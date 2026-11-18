"""USP Polymers video renderer (v3).

Reads script.json (scenes from Gemini), records the whole voiceover in one go
so the pace stays even, times every scene to the moment its sentence is spoken,
animates template.html frame by frame and writes output.mp4.

Usage:  python render.py script.json output.mp4 [--no-tts]
"""
import asyncio, bisect, json, os, re, shutil, subprocess, sys, tempfile

FPS = 30
VOICES = {"andrew": "en-US-AndrewMultilingualNeural", "emma": "en-US-EmmaMultilingualNeural"}
RATE = "-5%"
COMPANY = "USP Polymers LLP"
HERE = os.path.dirname(os.path.abspath(__file__))
LEAD = 0.45        # silence before the first word
LEAD_IN = 0.12     # a scene appears this long before its first word
END_HOLD = 1.8     # end card stays this long after the last word
MIN_DUR = {"big_text": 2.6, "big_number": 2.9, "reviews": 4.6, "checklist": 4.4,
           "spec_card": 4.6, "compare": 4.8, "end_card": 3.6}


# ---------- tools ----------
def find_ffmpeg():
    """Use the system ffmpeg if there is one, else the copy that comes with pip's imageio-ffmpeg."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = find_ffmpeg()


def ff(args):
    subprocess.run([FFMPEG, "-y", "-v", "error"] + args, check=True)


def duration(path):
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", out.stderr)
    if not m:
        raise RuntimeError(f"could not read the length of {path}")
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se)


def launch_browser(p):
    """Use the Google Chrome already on GitHub's machines; fall back to Playwright's own Chromium."""
    try:
        return p.chromium.launch(channel="chrome")
    except Exception as e:
        print(f"Chrome not available ({e.__class__.__name__}), using Playwright Chromium", flush=True)
        return p.chromium.launch()


# ---------- script ----------
def full_name(text):
    """Always say the full company name: USP / USP Polymers -> USP Polymers LLP."""
    return re.sub(r"\bUSP(?:\s+Polymers)?(?:\s+LLP)?\b", COMPANY, text)


def pick_voice(raw):
    """Voice from the script ("andrew" or "emma"); otherwise alternate by sheet row."""
    v = str(raw.get("voice") or "").strip().lower()
    if v in VOICES:
        return VOICES[v]
    try:
        return VOICES["emma"] if int(raw.get("row")) % 2 == 0 else VOICES["andrew"]
    except (TypeError, ValueError):
        return VOICES["andrew"]


def clean_script(raw):
    scenes = raw.get("scenes") or []
    if isinstance(scenes, str):
        scenes = json.loads(scenes)
    out = []
    for s in scenes:
        s = dict(s)
        s["type"] = (s.get("type") or "big_text").strip()
        for k in ("items", "left_items", "right_items"):
            v = s.get(k)
            if isinstance(v, str):
                s[k] = [x.strip() for x in re.split(r"\s*\|\s*", v) if x.strip()]
        for k in ("line1", "line2", "caption"):
            if s.get(k):
                s[k] = full_name(str(s[k]))
        v = str(s.get("voice") or "").strip()
        if v:
            v = full_name(v)
            if v[-1] not in ".!?":
                v += "."
        s["voice"] = v
        out.append(s)
    if not out or out[-1]["type"] != "end_card":
        out.append({"type": "end_card",
                    "voice": "Request your free trial sample from USP Polymers LLP at usppolymers dot in."})
    return {"scenes": out, "series": raw.get("series") or "USP Material Talks"}


# ---------- voice ----------
async def _speak(text, voice, path):
    import edge_tts
    words = []
    comm = edge_tts.Communicate(text, voice, rate=RATE, boundary="WordBoundary")
    with open(path, "wb") as f:
        async for c in comm.stream():
            if c["type"] == "audio":
                f.write(c["data"])
            elif c["type"] == "WordBoundary":
                words.append({"o": c["offset"] / 1e7, "d": c["duration"] / 1e7, "text": c["text"]})
    return words


def speak(text, voice, path):
    """Whole voiceover in one recording, with the time of every word.
    Retries, then falls back to the other voice. Returns (words, voice) or (None, None)."""
    others = [v for v in VOICES.values() if v != voice]
    for v in [voice, voice] + others:
        try:
            words = asyncio.run(asyncio.wait_for(_speak(text, v, path), timeout=150))
            if words and os.path.getsize(path) > 5000:
                return words, v
            print(f"voice attempt with {v} gave no word timings", flush=True)
        except Exception as e:
            print(f"voice attempt with {v} failed: {e!r}", flush=True)
    return None, None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def assign_scenes(words, scene_texts):
    """Match each spoken word to the scene its text came from."""
    starts, full = [], ""
    for t in scene_texts:
        starts.append(len(full))
        full += norm(t)
    cur = 0
    for w in words:
        n = norm(w["text"])
        pos = cur
        if n:
            i = full.find(n, cur)
            if i != -1 and i - cur <= 80:
                pos, cur = i, i + len(n)
        w["scene"] = max(0, bisect.bisect_right(starts, pos) - 1)
        # a scene with no text shares its start with the next one; skip empty scenes
        while w["scene"] + 1 < len(starts) and starts[w["scene"] + 1] == starts[w["scene"]]:
            w["scene"] += 1
    return words


def estimate_words(scene_texts):
    """Rough word timings for a run without voice (--no-tts or the voice service is down)."""
    words, t = [], 0.0
    for i, txt in enumerate(scene_texts):
        for tok in txt.split():
            d = 0.30 + 0.018 * len(tok)
            words.append({"o": t, "d": d, "text": tok, "scene": i})
            t += d + (0.38 if tok[-1] in ".!?" else 0.16 if tok[-1] in ",;:" else 0.02)
    return words, t + 0.3


def frames_preview(script, times, prefix):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = launch_browser(p)
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto("file://" + os.path.join(HERE, "template.html"))
        pg.wait_for_timeout(800)
        pg.evaluate("s => setup(s)", script)
        for t in times:
            pg.evaluate(f"render({t})")
            pg.screenshot(path=f"{prefix}_{t:05.2f}.jpg", type="jpeg", quality=88)
        b.close()


# ---------- main ----------
def main():
    src, dst = sys.argv[1], sys.argv[2]
    no_tts = "--no-tts" in sys.argv
    raw = json.load(open(src))
    voice_name = pick_voice(raw)
    script = clean_script(raw)
    scenes = script["scenes"]
    n = len(scenes)
    texts = [s["voice"] for s in scenes]
    work = tempfile.mkdtemp()
    full_mp3 = os.path.join(work, "voice_full.mp3")

    # 1. One recording for the whole video
    words = None
    if not no_tts and any(texts):
        words, used = speak(" ".join(t for t in texts if t), voice_name, full_mp3)
        if words:
            voice_name = used
            assign_scenes(words, texts)
            audio_len = duration(full_mp3)
    if not words:
        if not no_tts:
            print("WARNING: no voice available, making the video without a voiceover", flush=True)
        words, audio_len = estimate_words(texts)
        full_mp3 = None

    # 2. Where each scene's speech starts and ends inside the recording
    first = {}
    for w in words:
        first.setdefault(w["scene"], w["o"])
    seg_start = []
    for i in range(n):
        if i == 0:
            seg_start.append(0.0)
        elif i in first:
            seg_start.append(max(seg_start[-1], first[i] - LEAD_IN))
        else:
            seg_start.append(None)  # silent scene: fills in below
    for i in range(n - 1, -1, -1):
        if seg_start[i] is None:
            seg_start[i] = seg_start[i + 1] if i + 1 < n else audio_len
    seg_end = [seg_start[i + 1] if i + 1 < n else audio_len for i in range(n)]

    # 3. Scene lengths follow the speech; short scenes get a little extra time
    t = 0.0
    for i, s in enumerate(scenes):
        speech = seg_end[i] - seg_start[i] + (LEAD if i == 0 else 0)
        extra = END_HOLD if i == n - 1 else 0
        s["duration"] = round(max(MIN_DUR.get(s["type"], 3.0), speech + extra), 3)
        s["start"] = round(t, 3)
        s["shift"] = s["start"] + (LEAD if i == 0 else 0) - seg_start[i]  # recording time -> video time
        t += s["duration"]
        print(f"scene {i + 1}: {s['type']:<10} {s['duration']:.2f}s", flush=True)
    total = round(t, 3)
    script["total"] = total
    script["words"] = [{"t": round(w["o"] + scenes[w["scene"]]["shift"], 3), "d": round(w["d"], 3),
                        "text": w["text"], "scene": w["scene"]} for w in words]

    if "--frames" in sys.argv:  # preview: save a few still frames instead of the video
        frames_preview(script, [float(x) for x in sys.argv[sys.argv.index("--frames") + 1].split(",")], dst)
        return

    # 4. Animate the template frame by frame into a silent video
    from playwright.sync_api import sync_playwright
    silent = os.path.join(work, "silent.mp4")
    with sync_playwright() as p:
        b = launch_browser(p)
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto("file://" + os.path.join(HERE, "template.html"))
        pg.wait_for_timeout(800)
        pg.evaluate("s => setup(s)", script)
        enc = subprocess.Popen([FFMPEG, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS),
                                "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                                "-pix_fmt", "yuv420p", silent], stdin=subprocess.PIPE)
        for i in range(int(round(total * FPS))):
            pg.evaluate(f"render({i / FPS})")
            enc.stdin.write(pg.screenshot(type="jpeg", quality=92))
        enc.stdin.close()
        if enc.wait() != 0:
            sys.exit("video encoding failed")
        b.close()

    # 5. Voice track: each scene's slice of the one recording, placed at the scene's start
    parts = []
    for i, s in enumerate(scenes):
        part = os.path.join(work, f"a{i}.wav")
        length = seg_end[i] - seg_start[i]
        if full_mp3 and length > 0.05:
            delay = int(LEAD * 1000) if i == 0 else 0
            ff(["-ss", f"{seg_start[i]:.3f}", "-t", f"{length:.3f}", "-i", full_mp3, "-af",
                f"adelay={delay}|{delay},apad=whole_dur={s['duration']},atrim=0:{s['duration']}",
                "-ar", "44100", "-ac", "2", part])
        else:
            ff(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{s['duration']:.3f}", part])
        parts.append(part)
    lst = os.path.join(work, "a.txt")
    open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    voice = os.path.join(work, "voice.wav")
    ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", voice])

    # 6. Music: music.mp3 in the repo if present, else a soft generated pad
    music = os.path.join(HERE, "music.mp3")
    if os.path.exists(music):
        music_in = ["-stream_loop", "-1", "-i", music]
    else:
        music_in = ["-f", "lavfi", "-i",
                    "aevalsrc=0.05*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.1*t))+0.04*sin(2*PI*277.2*t)+0.035*sin(2*PI*329.6*t)"
                    f":s=44100:d={total + 1}"]
    ff(["-i", silent, "-i", voice] + music_in + [
        "-filter_complex",
        f"[2:a]volume=0.16,afade=t=in:d=1.5,afade=t=out:st={max(0, total - 2)}:d=2,atrim=0:{total}[m];"
        "[1:a]acompressor=threshold=-20dB:ratio=3:attack=5:release=120,loudnorm=I=-16:TP=-1.5:LRA=9,aresample=44100[v];"
        "[v][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-shortest", dst])
    print(f"voice: {voice_name if full_mp3 else 'none'}  ffmpeg: {FFMPEG}")
    print(f"done: {dst} ({total:.1f}s, {n} scenes)")


if __name__ == "__main__":
    main()
