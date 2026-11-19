# Components distributed with Journal Archive

Video tools are separate, unmodified programs/libraries. The Python application invokes FFmpeg as a subprocess on downloaded local files. Deno executes the yt-dlp EJS solver locally with its restricted permissions.

- Lucide 1.76.0 UI icons: https://www.npmjs.com/package/lucide-static/v/1.76.0 — ISC and MIT; see lucide.txt.
- yt-dlp 2026.10.21: https://github.com/yt-dlp/yt-dlp/tree/2026.10.21 — Unlicense; individual files may carry additional notices.
- yt-dlp-ejs 0.8.0: https://github.com/yt-dlp/ejs — Unlicense, including MIT/ISC components.
- Deno 2.9.8: https://github.com/denoland/deno/tree/v2.9.8 — MIT. See deno.txt. Runtime archives are pinned and verified against the SHA-256 digests of official release assets in packaging/video_runtime.py.
- imageio-ffmpeg 0.8.1: https://github.com/imageio/imageio-ffmpeg/tree/v0.8.1 — BSD-2-Clause; see imageio-ffmpeg.txt.
- FFmpeg binaries: GPL builds distributed with imageio-ffmpeg, with exact providers, versions, archive URLs, build identifiers, and checksums recorded in ffmpeg-sources.json. See ffmpeg-gpl3.txt. FFmpeg source: https://github.com/FFmpeg/FFmpeg . Build recipes and corresponding sources are available from the respective upstream build providers listed in that manifest (BtbN/FFmpeg-Builds, GyanD/codexffmpeg, evermeet.cx, osxexperts.net).

The archive itself contains user data and downloaded media; these software notices do not change their copyright or licensing.
