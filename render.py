"""USP Polymers video renderer.

Reads script.json (scenes from Gemini), makes a voiceover for each scene,
animates template.html in sync with the voice and writes output.mp4.

Usage:  python render.py script.json output.mp4 [--no-tts]
"""
import asyncio, json, os, re, subprocess, sys, tempfile

FPS = 30
VOICES = {"andrew": "en-US-AndrewMultilingualNeural", "emma": "en-US-EmmaMultilingualNeural"}
COMPANY = "USP Polymers LLP"
HERE = os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """Use the system ffmpeg if there is one, else the copy that comes with pip's imageio-ffmpeg."""
    import shutil
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = find_ffmpeg()


def run(cmd):
    if cmd and cmd[0] == "ffmpeg":
        cmd = [FFMPEG] + cmd[1:]
    subprocess.run(cmd, check=True)


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


async def _tts(text, path, voice):
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, voice, rate="-5%").save(path), timeout=60)


def tts(text, path, voice):
    """Make the voice for one scene. If the voice service is slow or fails,
    try again, then fall back to the other voice. Returns True if it worked."""
    others = [v for v in VOICES.values() if v != voice]
    for v in [voice, voice] + others:
        try:
            asyncio.run(_tts(text, path, v))
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                if v != voice:
                    print(f"note: used {v} for one scene (main voice did not answer)")
                return True
        except Exception as e:
            print(f"voice attempt with {v} failed: {e!r}", flush=True)
    return False


def pick_voice(raw):
    """Voice from the script ("andrew" or "emma"); otherwise alternate by sheet row."""
    v = str(raw.get("voice") or "").strip().lower()
    if v in VOICES:
        return VOICES[v]
    try:
        return VOICES["emma"] if int(raw.get("row")) % 2 == 0 else VOICES["andrew"]
    except (TypeError, ValueError):
        return VOICES["andrew"]


def full_name(text):
    """Always say the full company name: USP / USP Polymers -> USP Polymers LLP."""
    return re.sub(r"\bUSP(?:\s+Polymers)?(?:\s+LLP)?\b", COMPANY, text)


def clean_script(raw):
    scenes = raw.get("scenes") or []
    if isinstance(scenes, str):
        scenes = json.loads(scenes)
    out = []
    for s in scenes:
        s = dict(s)
        s["type"] = (s.get("type") or "big_text").strip()
        if s.get("voice"):
            s["voice"] = full_name(str(s["voice"]))
        for k in ("items", "left_items", "right_items"):
            v = s.get(k)
            if isinstance(v, str):
                s[k] = [x.strip() for x in re.split(r"\s*\|\s*", v) if x.strip()]
        out.append(s)
    if not out or out[-1]["type"] != "end_card":
        out.append({"type": "end_card", "voice": "Request your free trial sample from USP Polymers LLP at usppolymers dot in."})
    return {"scenes": out}


def main():
    src, dst = sys.argv[1], sys.argv[2]
    no_tts = "--no-tts" in sys.argv
    raw = json.load(open(src))
    voice_name = pick_voice(raw)
    script = clean_script(raw)
    work = tempfile.mkdtemp()

    # 1. Voice per scene, and scene length = voice length + a short pause
    voice_files = []
    for i, s in enumerate(script["scenes"]):
        text = (s.get("voice") or "").strip()
        vf = os.path.join(work, f"v{i}.mp3")
        if text and not no_tts and tts(text, vf, voice_name):
            d = duration(vf)
        else:
            d = max(1.5, len(text.split()) / 2.6)
            vf = None
        minimum = 4.5 if s["type"] in ("reviews", "checklist", "spec_card", "compare") else 3.2
        s["duration"] = round(max(minimum, d + 0.9), 2)
        print(f"scene {i + 1}: {s['type']} {s['duration']}s", flush=True)
        voice_files.append(vf)
    total = sum(s["duration"] for s in script["scenes"])

    # 2. Animate the template frame by frame into a silent video
    from playwright.sync_api import sync_playwright
    silent = os.path.join(work, "silent.mp4")
    with sync_playwright() as p:
        b = launch_browser(p)
        pg = b.new_page(viewport={"width": 1080, "height": 1350})
        pg.goto("file://" + os.path.join(HERE, "template.html"))
        pg.wait_for_timeout(800)
        pg.evaluate("s => setup(s)", script)
        ff = subprocess.Popen([FFMPEG, "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(FPS),
                               "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                               "-pix_fmt", "yuv420p", silent], stdin=subprocess.PIPE)
        for i in range(int(total * FPS)):
            pg.evaluate(f"render({i / FPS})")
            ff.stdin.write(pg.screenshot(type="jpeg", quality=92))
        ff.stdin.close()
        if ff.wait() != 0:
            sys.exit("video encoding failed")
        b.close()

    # 3. Voice track: each scene's voice padded to the scene length
    parts = []
    for i, s in enumerate(script["scenes"]):
        part = os.path.join(work, f"a{i}.wav")
        if voice_files[i]:
            run(["ffmpeg", "-y", "-v", "error", "-i", voice_files[i], "-af",
                 f"adelay=250|250,apad=whole_dur={s['duration']},atrim=0:{s['duration']}",
                 "-ar", "44100", "-ac", "2", part])
        else:
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                 "-t", str(s["duration"]), part])
        parts.append(part)
    lst = os.path.join(work, "a.txt")
    open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
    voice = os.path.join(work, "voice.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", voice])

    # 4. Music: music.mp3 in the repo if present, else a soft generated pad
    music = os.path.join(HERE, "music.mp3")
    if os.path.exists(music):
        music_in = ["-stream_loop", "-1", "-i", music]
    else:
        music_in = ["-f", "lavfi", "-i",
                    "aevalsrc=0.05*sin(2*PI*220*t)*(0.6+0.4*sin(2*PI*0.1*t))+0.04*sin(2*PI*277.2*t)+0.035*sin(2*PI*329.6*t)"
                    f":s=44100:d={total + 1}"]
    run(["ffmpeg", "-y", "-v", "error", "-i", silent, "-i", voice] + music_in + [
        "-filter_complex",
        f"[2:a]volume=0.18,afade=t=in:d=1.5,afade=t=out:st={max(0, total - 2)}:d=2,atrim=0:{total}[m];"
        "[1:a]acompressor=threshold=-20dB:ratio=3:attack=5:release=120,loudnorm=I=-16:TP=-1.5:LRA=9,aresample=44100[v];"
        "[v][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-shortest", dst])
    print(f"voice: {voice_name}  ffmpeg: {FFMPEG}")
    print(f"done: {dst} ({total:.1f}s, {len(script['scenes'])} scenes)")


if __name__ == "__main__":
    main()
