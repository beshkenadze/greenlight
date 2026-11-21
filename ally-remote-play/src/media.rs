use crate::journal;
use crate::xbox::{Console, Resolution, Result, Xbox};
use serde_json::{Value, json};
use std::{
    env, fs, io::{BufRead, BufReader, Write},
    path::PathBuf, process::{Child, Command, Stdio},
    sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc},
    thread, time::{Duration, Instant},
};

#[derive(Clone, Copy)]
pub struct VideoWindow {
    pub xid: u64,
    pub top_inset: u32,
}

struct Player(Child);

impl Drop for Player {
    fn drop(&mut self) {
        if let Some(mut stdin) = self.0.stdin.take() {
            let _ = writeln!(stdin, "{{\"action\":\"stop\"}}");
        }
        for _ in 0..15 {
            if self.0.try_wait().ok().flatten().is_some() { return; }
            thread::sleep(Duration::from_millis(40));
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn player_script() -> Result<PathBuf> {
    if let Some(path) = env::var_os("ALLY_MEDIA_WORKER") { return Ok(path.into()); }
    let exe = env::current_exe().map_err(|_| "Cannot locate the application")?;
    let sibling = exe.parent().ok_or("Cannot locate media files")?.join("gstreamer_player.py");
    if sibling.is_file() { return Ok(sibling); }
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("media/gstreamer_player.py");
    if source.is_file() { Ok(source) } else { Err("GStreamer worker file is missing".into()) }
}

fn launch(window: Option<VideoWindow>) -> Result<Player> {
    let python = env::var_os("ALLY_GST_PYTHON")
        .unwrap_or_else(|| if cfg!(target_os="windows") { "python.exe".into() } else { "python3".into() });
    let mut cmd = Command::new(python);
    cmd.arg(player_script()?).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    if let Some(window) = window {
        cmd.env("GREENLIGHT_VIDEO_PARENT_XID", window.xid.to_string())
            .env("GREENLIGHT_VIDEO_PARENT_INSET", window.top_inset.to_string());
    }
    if let Some(root) = env::var_os("ALLY_GST_RUNTIME") {
        let root = PathBuf::from(root);
        if !root.is_dir() { return Err("GStreamer runtime directory is missing".into()); }
        let mut plugins = vec![root.join("gstreamer-1.0")];
        if let Some(value) = env::var_os("GST_PLUGIN_PATH") { plugins.extend(env::split_paths(&value)); }
        cmd.env("GST_PLUGIN_PATH", env::join_paths(plugins).map_err(|_| "Invalid GStreamer plugin path")?);
        let variable = if cfg!(target_os="windows") { "PATH" }
            else if cfg!(target_os="macos") { "DYLD_LIBRARY_PATH" } else { "LD_LIBRARY_PATH" };
        let mut libs = vec![root];
        if let Some(value) = env::var_os(variable) { libs.extend(env::split_paths(&value)); }
        cmd.env(variable, env::join_paths(libs).map_err(|_| "Invalid GStreamer library path")?);
    }
    cmd.spawn().map(Player).map_err(|_| "Could not launch Python/GStreamer. Check the runtime installation.".into())
}

fn command(player: &mut Player, message: Value) -> Result<()> {
    writeln!(player.0.stdin.as_mut().ok_or("Media input pipe closed")?, "{message}")
        .map_err(|_| "GStreamer worker stopped".to_string())
}

pub fn play(xbox: Arc<Xbox>, console: Console, resolution: Resolution, window: Option<VideoWindow>,
            cancel: Arc<AtomicBool>, status: impl Fn(String)) -> Result<()> {
    status("Preparing Xbox session…".into());
    let (requested_width, requested_height) = resolution.dimensions();
    journal::record("stream_requested", json!({"bitrate_limit_kbps": 20480, "width": requested_width, "height": requested_height}));
    let path = xbox.reserve(&console, resolution)?;
    let result = (|| {
        xbox.provision(&path, &cancel)?;
        journal::record("stream_provisioned", json!({}));
        let mut player = launch(window)?;
        let output = player.0.stdout.take().ok_or("Missing GStreamer output pipe")?;
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(output).lines().map_while(std::result::Result::ok) {
                if let Ok(event) = serde_json::from_str::<Value>(&line) {
                    if tx.send(event).is_err() { break; }
                }
            }
        });
        command(&mut player, json!({"action":"start","bitrate":20480,"width":requested_width,"height":requested_height}))?;
        status("Negotiating the GStreamer connection…".into());
        let start = Instant::now();
        let mut keepalive = Instant::now();
        let mut last_stats = Instant::now();
        let mut controller = crate::input::Controller::new();
        let mut answer_set = false;
        let mut candidates = None;
        let mut connected = false;
        while !cancel.load(Ordering::Relaxed) {
            if !connected && start.elapsed() > Duration::from_secs(35) {
                journal::record("first_video_timeout", json!({}));
                return Err("GStreamer did not receive video before the timeout".into());
            }
            if keepalive.elapsed() >= Duration::from_secs(30) {
                xbox.keepalive(&path)?;
                keepalive = Instant::now();
            }
            if connected && last_stats.elapsed() >= Duration::from_secs(5) {
                command(&mut player, json!({"action":"stats"}))?;
                last_stats = Instant::now();
            }
            if let Some(state) = controller.poll() { command(&mut player, state)?; }
            let poll_ms = if cfg!(any(target_os = "windows", target_os = "macos")) { 8 } else { 150 };
            match rx.recv_timeout(Duration::from_millis(poll_ms)) {
                Ok(event) => match event.get("event").and_then(Value::as_str) {
                    Some("offer") => {
                        let offer = event.get("sdp").and_then(Value::as_str).ok_or("GStreamer returned no SDP offer")?;
                        let answer = xbox.sdp(&path, offer)?;
                        command(&mut player, json!({"action":"answer","sdp":answer}))?;
                    }
                    Some("answer-set") => answer_set = true,
                    Some("candidates") => candidates = event.get("candidates").cloned(),
                    Some("video") => {
                        connected = true;
                        let width = event.get("width").and_then(Value::as_u64).unwrap_or_default();
                        let height = event.get("height").and_then(Value::as_u64).unwrap_or_default();
                        let decoder = event.get("decoder").and_then(Value::as_str).unwrap_or("GStreamer");
                        let decoder = match decoder {
                            "vah264dec" | "avdec_h264" | "vtdec" | "vtdec_hw" | "d3d11h264dec" | "d3d12h264dec" => decoder,
                            _ => "unknown",
                        };
                        journal::record("video_active", json!({"width": width, "height": height, "decoder": decoder,
                            "requested_width": requested_width, "requested_height": requested_height}));
                        status(format!("{width}×{height} · {decoder}"));
                    }
                    Some("stats") => {
                        let count = |key| event.get(key).and_then(Value::as_u64).unwrap_or_default();
                        journal::record("video_stats", json!({
                            "requested_height": requested_height, "width": count("width"), "height": count("height"),
                            "worker_time_ms": event.get("time").and_then(Value::as_f64),
                            "encoded_bytes": count("encodedBytes"), "decoded_frames": count("decodedFrames"),
                            "rendered_frames": count("renderedFrames"), "sink_dropped": count("sinkDropped"),
                        }));
                    }
                    Some("error") => {
                        journal::record("gstreamer_error", json!({}));
                        return Err(event.get("message").and_then(Value::as_str)
                            .unwrap_or("GStreamer playback failed").to_owned());
                    }
                    _ => {}
                },
                Err(mpsc::RecvTimeoutError::Timeout) => {},
                Err(mpsc::RecvTimeoutError::Disconnected) => return Err("GStreamer worker closed".into()),
            }
            if answer_set && candidates.is_some() {
                let remote = xbox.ice(&path, candidates.take().unwrap_or_default())?;
                command(&mut player, json!({"action":"ice","candidates":remote}))?;
            }
        }
        Ok(())
    })();
    xbox.stop(&path);
    result
}

pub fn focus_gamescope(xid: u64) {
    if !cfg!(target_os="linux") || env::var("ALLY_GAMESCOPE_FOCUS").as_deref() != Ok("1") { return; }
    thread::spawn(move || {
        let name = "GAMESCOPECTRL_BASELAYER_WINDOW";
        if Command::new("xprop").args(["-root", name]).output().ok()
            .is_some_and(|out| !String::from_utf8_lossy(&out.stdout).contains("not found")) {
            let xid = xid.to_string();
            let _ = Command::new("xprop").args(["-id", &xid, "-f", "STEAM_GAME", "32c", "-set", "STEAM_GAME", "413095"]).output();
            let _ = Command::new("xprop").args(["-root", "-f", name, "32c", "-set", name, &xid]).output();
        }
    });
}

pub fn runtime_available() -> bool {
    player_script().is_ok_and(|path| fs::metadata(path).is_ok_and(|metadata| metadata.is_file()))
}
