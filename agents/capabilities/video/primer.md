# Video Tooling — primer

You have `video-probe`, `video-contact-sheet`, `video-scene-detect`,
`video-convert`, `ffmpeg`, and `ffprobe` on PATH, backed by a host-installed,
hash-pinned static ffmpeg. No installs are needed or permitted in-session.

Typical uses:

- **Verify browser animation**: set the viewport, run
  `agent-browser record start /workspace/output/transition.webm`, perform one
  interaction, and run `agent-browser record stop`. The recorder preserves the
  active browser session's cookies and storage. Always stop it so ffmpeg can
  finalize the file; then make a contact sheet from that short recording.

- **Understand a recording before watching it**: `video-probe` for
  duration/dimensions, then `video-contact-sheet rec.mp4 sheet.png` and read
  the sheet as one image.
- **Find the moments that matter in a screen recording**:
  `video-scene-detect rec.mp4 --threshold 0.1 --frames-dir /tmp/scenes` —
  UI transitions (a sheet opening, a page change) score low; start near 0.1
  and lower it if cuts are missing.
- **Compile an evidence step gallery into an animation**:
  `video-convert --frames '/path/steps/*.png' out.mp4 --fps 1` (files sort
  lexically; evidence-pipeline step files are numbered so order is correct).
- **Make something embeddable**: `video-convert in.mp4 out.gif`.

Write outputs to `/workspace/output/` when they should outlive the session.
