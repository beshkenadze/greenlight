mod input;
mod media;
mod xbox;

use gpui_kit::component::{button::{Button, ButtonVariants}, label::Label};
use gpui_kit::*;
use raw_window_handle::{HasWindowHandle, RawWindowHandle};
use std::{sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc}, thread, time::Duration};

enum Event {
    Device { url: String, code: String },
    Account { xbox: Arc<xbox::Xbox>, consoles: Vec<xbox::Console> },
    SignInEnded(xbox::Result<()>),
    StreamStatus(String),
    StreamEnded(xbox::Result<()>),
}

struct RemotePlay {
    tx: mpsc::Sender<Event>,
    account: Option<Arc<xbox::Xbox>>,
    consoles: Vec<xbox::Console>,
    sign_in: Option<Arc<AtomicBool>>,
    stream: Option<Arc<AtomicBool>>,
    device: Option<(String, String)>,
    status: String,
}

impl Drop for RemotePlay {
    fn drop(&mut self) {
        if let Some(cancel) = &self.sign_in { cancel.store(true, Ordering::Relaxed); }
        if let Some(cancel) = &self.stream { cancel.store(true, Ordering::Relaxed); }
    }
}

fn x11_window(window: &Window) -> Option<media::VideoWindow> {
    let handle = HasWindowHandle::window_handle(window).ok()?;
    let xid = match handle.as_raw() {
        RawWindowHandle::Xcb(handle) => u64::from(handle.window.get()),
        RawWindowHandle::Xlib(handle) => handle.window,
        _ => return None,
    };
    Some(media::VideoWindow { xid, top_inset: (58.0 * window.scale_factor()).ceil() as u32 })
}

impl RemotePlay {
    fn start_login(&mut self) {
        if self.sign_in.is_some() || self.stream.is_some() { return; }
        let cancel = Arc::new(AtomicBool::new(false));
        self.sign_in = Some(cancel.clone());
        self.device = None;
        self.status = "Requesting Microsoft sign-in…".into();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = (|| {
                let client = xbox::client()?;
                let client_id = xbox::public_client_id();
                let device = xbox::begin_sign_in(&client, &client_id)?;
                let _ = tx.send(Event::Device { url: device.verification_uri.clone(), code: device.user_code.clone() });
                let xbox = Arc::new(xbox::finish_sign_in(client, client_id, device, &cancel)?);
                let consoles = xbox.consoles()?;
                let _ = tx.send(Event::Account { xbox, consoles });
                Ok(())
            })();
            let _ = tx.send(Event::SignInEnded(result));
        });
    }

    fn start_stream(&mut self, console: xbox::Console, window: Option<media::VideoWindow>) {
        if self.stream.is_some() || self.sign_in.is_some() { return; }
        let Some(account) = self.account.clone() else { return; };
        let cancel = Arc::new(AtomicBool::new(false));
        self.stream = Some(cancel.clone());
        self.status = "Connecting to Xbox…".into();
        let tx = self.tx.clone();
        thread::spawn(move || {
            let result = media::play(account, console, window, cancel, |status| { let _ = tx.send(Event::StreamStatus(status)); });
            let _ = tx.send(Event::StreamEnded(result));
        });
    }

    fn receive(&mut self, event: Event) {
        match event {
            Event::Device { url, code } => {
                self.status = "Waiting for Microsoft confirmation".into();
                self.device = Some((url, code));
            }
            Event::Account { xbox, consoles } => {
                self.status = if consoles.is_empty() { "No Xbox available for Remote Play".into() } else { "Choose an Xbox".into() };
                self.account = Some(xbox);
                self.consoles = consoles;
            }
            Event::SignInEnded(result) => {
                self.sign_in = None;
                self.device = None;
                if let Err(error) = result { self.status = error; }
            }
            Event::StreamStatus(status) => {
                self.status = status;
            }
            Event::StreamEnded(result) => {
                self.stream = None;
                self.status = result.err().unwrap_or_else(|| "Stream stopped".into());
            }
        }
    }
}

impl Render for RemotePlay {
    fn render(&mut self, _: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        let sign_button = Button::new("sign-in").primary().label("Sign in with Microsoft")
            .on_click(cx.listener(|view, _, _, cx| { view.start_login(); cx.notify(); }));
        let stop_button = Button::new("stop").label("Stop stream")
            .on_click(cx.listener(|view, _, _, cx| {
                if let Some(cancel) = &view.stream { cancel.store(true, Ordering::Relaxed); }
                view.status = "Stopping stream…".into();
                cx.notify();
            }));
        let mut header = div().h(px(58.)).px_4().flex().items_center().justify_between()
            .child(Label::new(self.status.clone()));
        if self.stream.is_some() { header = header.child(stop_button); }
        else if self.sign_in.is_none() { header = header.child(sign_button); }
        let mut content = div().flex_1().p_4().flex().flex_col().gap_3().bg(rgb(0x13161a)).text_color(rgb(0xf5f5f5));
        if let Some((url, code)) = &self.device {
            let url = url.clone();
            content = content
                .child(div().child("Open Microsoft device sign-in and enter this code:"))
                .child(div().text_xl().child(code.clone()))
                .child(Button::new("open-microsoft").primary().label("Open Microsoft")
                    .on_click(move |_, _, cx| cx.open_url(&url)))
                .child(Button::new("cancel-login").label("Cancel")
                    .on_click(cx.listener(|view, _, _, _| { if let Some(cancel) = &view.sign_in { cancel.store(true, Ordering::Relaxed); } })));
        }
        if self.stream.is_none() {
            for (index, console) in self.consoles.iter().enumerate() {
                let name = if console.name.is_empty() { console.model.clone() } else { console.name.clone() };
                let label = format!("{name} · {} · {}", console.power, console.id);
                let selected = console.clone();
                content = content.child(Button::new(("console", index)).label(label)
                    .on_click(cx.listener(move |view, _, window, cx| {
                        view.start_stream(selected.clone(), x11_window(window));
                        cx.notify();
                    })));
            }
        }
        div().size_full().flex().flex_col().child(header).child(content)
    }
}

fn main() {
    let (tx, rx) = mpsc::channel();
    application().with_assets(assets::Assets).run(|cx| {
        init(cx);
        let bounds = Bounds::centered(None, size(px(1120.), px(690.)), cx);
        open_window(WindowOptions {
            titlebar: Some(TitlebarOptions { title: Some("Ally Remote Play".into()), ..Default::default() }),
            window_bounds: Some(WindowBounds::Windowed(bounds)),
            ..Default::default()
        }, cx, |window, cx| {
            if let Some(window) = x11_window(window) { media::focus_gamescope(window.xid); }
            cx.new(|cx| {
                cx.spawn(async move |view, cx| loop {
                    cx.background_executor().timer(Duration::from_millis(120)).await;
                    while let Ok(event) = rx.try_recv() {
                        if view.update(cx, |view: &mut RemotePlay, cx| { view.receive(event); cx.notify(); }).is_err() { return; }
                    }
                }).detach();
                RemotePlay { tx, account: None, consoles: Vec::new(), sign_in: None, stream: None, device: None,
                    status: if media::runtime_available() { "Ally Remote Play".into() } else { "GStreamer worker missing".into() } }
            })
        }).expect("Could not open the GPUI window");
    });
}
