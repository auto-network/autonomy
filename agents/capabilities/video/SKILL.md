---
name: video
description: Video tooling capability. Capture agent-browser sessions, probe, contact-sheet, scene-detect and convert video files, and compile an image sequence into an animation, using a sha256-pinned static ffmpeg/ffprobe mounted read-only.
---

# Video Tooling

Capture rendered agent-browser output; probe, contact-sheet, scene-detect, and
convert video files; and compile an image sequence (an evidence step gallery)
into an animation with zero per-session installs. The binaries are a
sha256-pinned static ffmpeg/ffprobe, installed once per host and mounted
read-only.

## Capture browser motion

Use agent-browser's recorder around the exact interaction being evaluated. It
creates a fresh browser context while preserving the active session's cookies
and local storage. Set the viewport before recording and write lasting evidence
to `/workspace/output/`:

```bash
agent-browser set viewport 390 844
agent-browser open https://localhost:8080/sessions
agent-browser record start /workspace/output/nav-open.webm
agent-browser click '#nav-toggle'
agent-browser wait 700
agent-browser record stop
```

The recorder calls `ffmpeg` when it stops; this capability exposes its pinned
binary on `PATH` for that purpose. Always run `record stop`, including after a
failed interaction, or the output is not finalized. Prefer a short recording
of one transition over a long browsing session, then inspect it with
`video-contact-sheet` and `video-scene-detect`.

## Commands

```bash
video-probe <file>
    # format + stream facts as JSON (ffprobe -show_format -show_streams)

video-contact-sheet <in-video> <out-image> [--tiles 4x3] [--width 320]
    # one image tiling evenly spaced frames across the whole duration

video-scene-detect <in-video> [--threshold 0.3] [--frames-dir DIR]
    # prints "<timestamp-seconds> <score>" per detected cut;
    # --frames-dir also saves one PNG per cut. Screen recordings are
    # low-motion: thresholds around 0.05–0.15 find UI transitions that
    # the 0.3 default misses.

video-convert <in-video> <out.{mp4,webm,gif}>
    # format conversion (gif output is downscaled to 640px, 8fps)

video-convert --frames '<glob>' <out.{mp4,gif}> [--fps N]
    # compile matching images, sorted, into a video; N frames per second
    # (default 1 — one second per step, the evidence-gallery case).
    # Quote the glob so the shell does not expand it.
```

## If the tools refuse to run

Exit code 3 with "not provisioned", or `agent-browser record stop` reporting
that ffmpeg is unavailable, means the host-install has not run on this host or
the workspace did not enable `video_tooling`. The pinned ffmpeg is populated by
the Capability Host-Install Runner (protocol graph://149705db-a39), never by
sessions.
Report it; do not install ffmpeg yourself. On the host, an operator runs
the runner on demand with `graph capability host-install autonomy/video`
(idempotent: it fingerprints `install/ffmpeg.pin` and skips when current).

## If the host install fails with a hash mismatch

The pin's url is upstream's rolling "latest release" archive (no versioned
archive exists there), so the sha256 in `install/ffmpeg.pin` stops matching
whenever upstream publishes a new build. A hash mismatch here therefore
usually means UPSTREAM RELEASED, not compromise — the install failing
closed is correct. The fix is a deliberate pin refresh: download the new
tarball, verify it runs, update url-implied version + sha256 in
`ffmpeg.pin` in one reviewed commit. The changed pin re-fingerprints the
install, so the runner reinstalls everywhere on its next pass.
