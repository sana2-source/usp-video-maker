# USP Polymers – video maker

Turns a scene-by-scene script (written by Gemini in Make) into a branded 4:5 explainer video with an Indian English voiceover.

- `template.html` – the animated scene designs (big text, reviews, checklist, spec card, compare, big number, end card)
- `render.py` – makes the voice (Microsoft Edge neural voice, free), animates the template and adds music
- `.github/workflows/render.yml` – runs on GitHub when Make sends a script; the finished MP4 is attached to the release called `videos`
- `music.mp3` (optional) – put your own royalty-free track here; otherwise a soft generated tone is used

Test without Make: Actions tab → "Render video" → Run workflow. The result appears under Releases → videos.
