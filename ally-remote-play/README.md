# Ally Remote Play

Standalone Xbox Remote Play client. The desktop interface, Microsoft device-code
login, Xbox discovery, and session signaling run in Rust and GPUI Kit. A local
PyGObject worker runs GStreamer WebRTC, audio, hardware decode, and presentation.
The worker receives SDP and ICE over a subprocess pipe. OAuth and Xbox
credentials are kept in memory in the Rust process. No Electron, browser
WebRTC, or Greenlight application process is required. The system browser is
used only to confirm the Microsoft login.

## Linux / SteamOS

Install GStreamer, PyGObject with the Gst/GstWebRTC/GstSdp/GstVideo typelibs,
GStreamer WebRTC, ICE and SCTP plugins, H.264/Opus plugins, X11 video output,
and Python evdev. An AMD VA-API H.264 decoder is preferred. Run:

```sh
cargo run --locked
```

The media worker can be placed beside the executable or selected with
`ALLY_MEDIA_WORKER`. Set `ALLY_GST_PYTHON` for the interpreter and
`ALLY_GST_RUNTIME` for a private GStreamer library directory. On Gamescope,
`ALLY_GAMESCOPE_FOCUS=1` brings the X11 app window to the front. No SteamOS
system package changes are needed if these dependencies are installed in a
private runtime. The client defaults to a 1080p request at a 20 Mbit/s limit.
The resolution picker also offers 720p, 1440p, and experimental 800p and
900p requests. Xbox controls the transmitted size. The app and the journal
report the actual decoder dimensions separately from the requested size.

## Platform ports

- Linux uses X11/XWayland embedding, VA-API and evdev.
- Windows has decoder selection for D3D11/D3D12, generic GStreamer video/audio
  sinks, and a Rust gilrs gamepad adapter. It needs a matching MSVC GPUI build,
  Python/PyGObject and GStreamer runtime.
- macOS has decoder selection for VideoToolbox, generic GStreamer video/audio
  sinks, and the same gilrs adapter. It needs a matching GPUI build and the
  GStreamer/PyGObject runtime.

Windows and macOS are portability paths, not validated builds. The prototype
opens a separate GStreamer video window on non-X11 platforms. Validation must
cover dependency packaging, graphics sink behavior, permissions, gamepad
mapping, and playback before either port is distributed.

## Login and console discovery

Select **Sign in with Microsoft**, then scan the QR code or open Microsoft
sign-in in your browser. The Xbox consumer sign-in link prefills the code.
The code remains visible for manual entry if Microsoft requests it. After
confirming sign-in, select a console available to the signed-in Xbox account.
Power state and the console ID are displayed for identification. A
console ID does not register or link an Xbox to another account.

`XBOX_CLIENT_ID` can override the public OAuth app ID when deploying an
appropriately registered app. The default is the public Xbox client ID used
by the interoperable device flow. The app does not bundle a client secret,
copy any other application's credentials, or persist short-lived access
tokens. It saves the Microsoft refresh token locally and rotates it when
Microsoft returns a replacement. On Unix the session file is accessible only
to the current user (mode 0600). The file is not encrypted at rest. The
optional `ALLY_SESSION_FILE` environment variable overrides its location.
**Sign out** removes the stored session from this device. When Microsoft
expires or revokes the saved token, confirm sign-in again.

## Diagnostics

The app writes one JSON event per line to standard error. To save a local
journal, run the binary with `2>>ally-remote-play.log`, then follow it with
`tail -f ally-remote-play.log`. The journal contains sign-in and stream stages,
HTTP failure statuses, console counts, and negotiated video dimensions and
decoder. It omits codes, tokens, console IDs, and SDP/ICE payloads.
Run the binary with `--probe-resolutions` after signing in once to test 720p,
800p and 900p in sequence against a single available Xbox. The probe records
decoded frame sizes and five-second video counters and stops each stream
after 16 seconds of video.

The GStreamer worker is based on the MIT-licensed experimental media path
previously developed alongside Greenlight. See LICENSE for the original
Greenlight attribution. This standalone app does not link or invoke Greenlight.
