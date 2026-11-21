#!/usr/bin/env python3
"""Experimental xHome GStreamer player. Signaling uses JSON lines over stdio."""
import ctypes as c
import json
import os
import struct
import subprocess
import sys
import threading
import time
import uuid

import gi
for namespace in ('Gst', 'GstSdp', 'GstWebRTC', 'GstVideo'):
    gi.require_version(namespace, '1.0')
from gi.repository import GLib, Gst, GstSdp, GstVideo, GstWebRTC

Gst.init(None)

BUTTONS = {304: 16, 305: 32, 307: 64, 308: 128, 310: 4096, 311: 8192,
           314: 8, 315: 4, 316: 2, 317: 16384, 318: 32768}
AXES = (0, 1, 3, 4)


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


def milliseconds():
    return time.monotonic_ns() / 1_000_000


def checked(promise, name):
    if promise.wait() != Gst.PromiseResult.REPLIED:
        raise RuntimeError(name)
    reply = promise.get_reply()
    if reply is not None and reply.has_field('error'):
        raise RuntimeError(name)
    return reply


class X11WindowAttributes(c.Structure):
    _fields_ = [('background_pixmap', c.c_ulong), ('background_pixel', c.c_ulong),
                ('border_pixmap', c.c_ulong), ('border_pixel', c.c_ulong),
                ('bit_gravity', c.c_int), ('win_gravity', c.c_int), ('backing_store', c.c_int),
                ('backing_planes', c.c_ulong), ('backing_pixel', c.c_ulong),
                ('save_under', c.c_int), ('event_mask', c.c_long),
                ('do_not_propagate_mask', c.c_long), ('override_redirect', c.c_int),
                ('colormap', c.c_ulong), ('cursor', c.c_ulong)]


class X11VideoWindow:
    def __init__(self, parent, on_close):
        self.x11 = c.CDLL('libX11.so.6')
        ptr, xid, int_, uint = c.c_void_p, c.c_ulong, c.c_int, c.c_uint
        pointer = c.POINTER
        for name, result, args in (
            ('XOpenDisplay', ptr, [c.c_char_p]),
            ('XDefaultRootWindow', xid, [ptr]),
            ('XDefaultScreen', int_, [ptr]),
            ('XDefaultDepth', int_, [ptr, int_]),
            ('XDefaultVisual', ptr, [ptr, int_]),
            ('XDefaultColormap', xid, [ptr, int_]),
            ('XCreateWindow', xid, [ptr, xid, int_, int_, uint, uint, uint, int_, uint, ptr, xid,
                pointer(X11WindowAttributes)]),
            ('XGetGeometry', int_, [ptr, xid, pointer(xid), pointer(int_), pointer(int_),
                pointer(uint), pointer(uint), pointer(uint), pointer(uint)]),
            ('XCreateSimpleWindow', xid, [ptr, xid, int_, int_, uint, uint, uint, xid, xid]),
            ('XMoveResizeWindow', int_, [ptr, xid, int_, int_, uint, uint]),
            ('XStoreName', int_, [ptr, xid, c.c_char_p]),
            ('XMapRaised', int_, [ptr, xid]),
            ('XFlush', int_, [ptr]),
            ('XInternAtom', xid, [ptr, c.c_char_p, int_]),
            ('XSetWMProtocols', int_, [ptr, xid, pointer(xid), int_]),
            ('XPending', int_, [ptr]),
            ('XNextEvent', int_, [ptr, ptr]),
            ('XDestroyWindow', int_, [ptr, xid]),
            ('XCloseDisplay', int_, [ptr]),
        ):
            function = getattr(self.x11, name)
            function.restype, function.argtypes = result, args
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('Native playback requires an X11 display')
        self.parent = parent or self.x11.XDefaultRootWindow(self.display)
        self.inset = int(os.getenv('GREENLIGHT_VIDEO_PARENT_INSET', '162')) if parent else 0
        self.on_close = on_close
        self.size = self.dimensions()
        if parent:
            # GPUI's Xwayland window is ARGB32. ximagesink creates its image
            # for the screen's default visual, so the child must use that depth.
            screen = self.x11.XDefaultScreen(self.display)
            attributes = X11WindowAttributes(colormap=self.x11.XDefaultColormap(self.display, screen))
            self.xid = self.x11.XCreateWindow(self.display, self.parent, 0, self.inset, *self.size, 0,
                self.x11.XDefaultDepth(self.display, screen), 1, self.x11.XDefaultVisual(self.display, screen),
                (1 << 1) | (1 << 3) | (1 << 13), c.byref(attributes))
        else:
            self.xid = self.x11.XCreateSimpleWindow(self.display, self.parent, 0, 0, *self.size, 0, 0, 0)
        self.x11.XStoreName(self.display, self.xid, b'Greenlight Native Video')
        delete = c.c_ulong(self.x11.XInternAtom(self.display, b'WM_DELETE_WINDOW', 0))
        self.x11.XSetWMProtocols(self.display, self.xid, c.byref(delete), 1)
        self.x11.XMapRaised(self.display, self.xid)
        self.x11.XFlush(self.display)
        self.prior_focus = None
        self.timer = GLib.timeout_add(120, self.update)
        self.focus_timer = 0
        if not parent and os.getenv('GREENLIGHT_GAMESCOPE_FOCUS') == '1':
            self.focus_timer = GLib.timeout_add(200, self.focus)

    def dimensions(self):
        root = c.c_ulong()
        x, y = c.c_int(), c.c_int()
        width, height, border, depth = (c.c_uint() for _ in range(4))
        self.x11.XGetGeometry(self.display, self.parent, c.byref(root), c.byref(x), c.byref(y),
                              c.byref(width), c.byref(height), c.byref(border), c.byref(depth))
        return max(1, width.value), max(1, height.value - self.inset)

    def update(self):
        while self.x11.XPending(self.display):
            event = (c.c_long * 24)()
            self.x11.XNextEvent(self.display, c.byref(event))
            if c.cast(event, c.POINTER(c.c_int))[0] == 33:  # WM_DELETE_WINDOW client message
                self.on_close()
                return GLib.SOURCE_CONTINUE
        size = self.dimensions()
        # The embedded surface follows the GPUI window size; the top-level
        # surface is sized by Gamescope.
        if self.inset and size != self.size:
            self.size = size
            self.x11.XMoveResizeWindow(self.display, self.xid, 0, self.inset, *size)
            self.x11.XFlush(self.display)
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def xprop(*arguments):
        try:
            return subprocess.run(['xprop', *arguments], capture_output=True,
                                  text=True, timeout=2, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ''

    def focus(self):
        self.focus_timer = 0
        name = 'GAMESCOPECTRL_BASELAYER_WINDOW'
        property_value = self.xprop('-root', name).rpartition(' = ')[2].strip()
        if property_value.isdecimal():
            self.prior_focus = property_value
            self.xprop('-id', str(self.xid), '-f', 'STEAM_GAME', '32c', '-set', 'STEAM_GAME', '413094')
            self.xprop('-root', '-f', name, '32c', '-set', name, str(self.xid))
        return GLib.SOURCE_REMOVE

    def close(self):
        GLib.source_remove(self.timer)
        if self.focus_timer:
            GLib.source_remove(self.focus_timer)
        name = 'GAMESCOPECTRL_BASELAYER_WINDOW'
        if self.prior_focus is not None and self.xprop('-root', name).rpartition(' = ')[2].strip() == str(self.xid):
            self.xprop('-root', '-f', name, '32c', '-set', name, self.prior_focus)
        self.x11.XDestroyWindow(self.display, self.xid)
        self.x11.XCloseDisplay(self.display)


class Player:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.pipeline = Gst.Pipeline.new('greenlight-native')
        self.rtc = Gst.ElementFactory.make('webrtcbin')
        if self.rtc is None:
            raise RuntimeError('GStreamer webrtcbin is unavailable')
        self.rtc.set_property('bundle-policy', GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.rtc.set_property('latency', 20)
        self.pipeline.add(self.rtc)
        self.rtc.connect('on-ice-candidate', self.candidate)
        self.rtc.connect('pad-added', self.link_track)
        self.rtc.connect('notify::connection-state', self.connection_changed)
        self.rtc.connect('notify::ice-gathering-state', self.gathering_changed)
        self.pipeline.get_bus().add_signal_watch()
        self.pipeline.get_bus().connect('message', self.bus_message)
        self.candidates = []
        self.channels = {}
        self.sequence = 0
        self.start_time = milliseconds()
        self.width = 0
        self.height = 0
        self.encoded_frames = 0
        self.encoded_bytes = 0
        self.decoded_frames = 0
        self.input_device = None
        self.held = {}
        self.axis_info = {}
        self.override = None
        self.trial = None
        self.frame_trial = None
        self.frame_timing = {}
        self.rtp_received = {}
        self.pending_rtp = None
        self.tracks = set()
        self.video_sink = None
        self.window_id = int(os.getenv('GREENLIGHT_VIDEO_XID', '0'), 0)
        self.window = None
        self.decoder_name = None
        self.external_pad = None

    def connect_gamepad(self):
        try:
            import evdev
        except ImportError:
            return
        preferred = os.getenv('GREENLIGHT_GAMEPAD')
        for path in ([preferred] if preferred else evdev.list_devices()):
            device = evdev.InputDevice(path)
            codes = device.capabilities(absinfo=False)
            if 304 in codes.get(1, []) and 3 in codes.get(3, []):
                self.input_device = device
                self.axis_info = {axis: device.absinfo(axis) for axis in codes[3]}
                break
            device.close()

    def handle(self, command):
        try:
            match command['action']:
                case 'start':
                    self.start(command['bitrate'], command.get('width', 1920), command.get('height', 1080))
                case 'answer':
                    _, sdp = GstSdp.SDPMessage.new_from_text(command['sdp'])
                    answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
                    promise = Gst.Promise.new()
                    self.rtc.emit('set-remote-description', answer, promise)
                    checked(promise, 'Remote description rejected')
                    emit('answer-set')
                case 'ice':
                    for candidate in command['candidates']:
                        value = candidate.get('candidate', '').removeprefix('a=')
                        if value.startswith('candidate:'):
                            self.rtc.emit('add-ice-candidate', int(candidate.get('sdpMLineIndex') or 0), value)
                case 'pulse':
                    self.override = (command.get('axis', 0), command.get('buttons', 0),
                                     milliseconds() + command.get('duration', 230))
                    if command.get('trial'):
                        self.trial = {'actuatedAt': milliseconds()}
                case 'trial':
                    if self.frame_trial is None:
                        self.begin_trial()
                case 'gamepad':
                    axes = command.get('axes')
                    if isinstance(axes, list) and len(axes) == 4:
                        self.external_pad = ([max(-32767, min(32767, int(axis))) for axis in axes],
                                             int(command.get('buttons', 0)) & 0xffff,
                                             max(0, min(65535, int(command.get('lt', 0)))),
                                             max(0, min(65535, int(command.get('rt', 0)))))
                case 'stats':
                    self.stats()
                case 'stop':
                    self.loop.quit()
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            emit('error', message=str(error))
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def start(self, bitrate, width, height):
        self.target_width, self.target_height = int(width), int(height)
        self.connect_gamepad()
        if not self.window_id and sys.platform.startswith('linux') and os.getenv('DISPLAY'):
            parent = int(os.getenv('GREENLIGHT_VIDEO_PARENT_XID', '0'), 0)
            self.window = X11VideoWindow(parent, self.loop.quit)
            self.window_id = self.window.xid
        direction = GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY
        for caps in (
            'application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,payload=111,encoding-params=(string)2',
            'application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=103,packetization-mode=(string)1,profile-level-id=(string)42001f',
        ):
            self.rtc.emit('add-transceiver', direction, Gst.Caps.from_string(caps))
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError('Cannot start pipeline')
        for label, protocol in [('input', '1.0'), ('control', 'controlV1'), ('message', 'messageV1'), ('chat', 'chatV1')]:
            options = Gst.Structure.new_empty('application/data-channel')
            options.set_value('protocol', protocol)
            options.set_value('ordered', True)
            channel = self.rtc.emit('create-data-channel', label, options)
            if channel is None:
                raise RuntimeError('Cannot create WebRTC data channel')
            self.channels[label] = channel
        message = self.channels['message']
        message.connect('on-open', lambda _: self.send_json('message',
            {'type': 'Handshake', 'version': 'messageV1', 'id': str(uuid.uuid4()), 'cv': ''}))
        message.connect('on-message-string', lambda _, data: self.message(data))
        message.connect('on-message-data', lambda _, data: self.message(data.get_data().decode()))
        promise = Gst.Promise.new()
        self.rtc.emit('create-offer', None, promise)
        reply = checked(promise, 'Offer creation failed')
        offer = reply.get_value('offer')
        sdp = offer.sdp.as_text()
        lines = sdp.splitlines()
        video = next(i for i, line in enumerate(lines) if line.startswith('m=video '))
        end = next((i for i in range(video + 1, len(lines)) if lines[i].startswith('m=')), len(lines))
        # Place bandwidth ahead of media attributes (RFC 4566 field order).
        before_attributes = next((i for i in range(video + 1, end) if lines[i].startswith('a=')), end)
        lines.insert(before_attributes, f'b=AS:{int(bitrate)}')
        sdp = '\r\n'.join(lines) + '\r\n'
        _, description = GstSdp.SDPMessage.new_from_text(sdp)
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, description)
        promise = Gst.Promise.new()
        self.rtc.emit('set-local-description', offer, promise)
        checked(promise, 'Local description rejected')
        emit('offer', sdp=sdp)

    def candidate(self, _, index, candidate):
        self.candidates.append({'candidate': candidate, 'sdpMLineIndex': index})

    def gathering_changed(self, *_):
        if self.rtc.get_property('ice-gathering-state') == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            emit('candidates', candidates=self.candidates)

    def connection_changed(self, *_):
        emit('connection', state=self.rtc.get_property('connection-state').value_nick)

    def send_json(self, label, data):
        channel = self.channels[label]
        channel.emit('send-data', GLib.Bytes.new(json.dumps(data, separators=(',', ':')).encode()))

    def message(self, data):
        if json.loads(data).get('type') == 'HandshakeAck':
            GLib.idle_add(self.handshake_ack)

    def handshake_ack(self):
        width, height = self.target_width, self.target_height
        dimensions = {'width': width, 'height': height}
        self.send_json('control', {'message': 'authorizationRequest',
            'accessKey': '4BDB3609-C1F1-4195-9B37-FEFF45DA8B8E'})
        self.send_json('control', {'message': 'gamepadChanged', 'gamepadIndex': 0, 'wasAdded': True})
        self.send_json('control', {'message': 'VideoPreference', 'type': 'VideoPreference',
            'resolution': dimensions, 'dimensions': dimensions, 'maxFPS': 60})
        self.send_json('control', {'message': 'userRequestedResolutionUpdate', 'resolutionAlias': str(height)})
        for target, content in (
            ('systemUi/configuration', {'version': [0, 1, 0], 'systemUis': []}),
            ('properties/clientappinstallidchanged', {'clientAppInstallId': str(uuid.uuid4())}),
            ('characteristics/orientationchanged', {'orientation': 0}),
            ('characteristics/touchinputenabledchanged', {'touchInputEnabled': False}),
            ('characteristics/clientdevicecapabilities', {}),
            ('characteristics/dimensionschanged', {'horizontal': width, 'vertical': height,
                'preferredWidth': width, 'preferredHeight': height, 'safeAreaLeft': 0, 'safeAreaTop': 0,
                'safeAreaRight': width, 'safeAreaBottom': height, 'supportsCustomResolution': True}),
        ):
            self.send_json('message', {'type': 'Message', 'target': '/streaming/' + target,
                'content': json.dumps(content), 'id': str(uuid.uuid4()), 'cv': ''})
        self.channels['input'].emit('send-data', GLib.Bytes.new(struct.pack('<HIdB', 8, 0, self.start_time, 2)))
        GLib.timeout_add(8, self.send_input)
        emit('input-ready')
        return GLib.SOURCE_REMOVE

    def send_input(self):
        channel = self.channels['input']
        if channel.get_property('ready-state') != GstWebRTC.WebRTCDataChannelState.OPEN:
            return GLib.SOURCE_REMOVE
        polled = milliseconds()
        if self.input_device:
            try:
                for event in self.input_device.read():
                    self.held[event.type, event.code] = event.value
            except BlockingIOError:
                pass
            except OSError:
                self.input_device.close()
                self.input_device = None
                self.held.clear()

        def value(code):
            info = self.axis_info.get(code)
            if not self.input_device or not info or info.max <= info.min:
                return 0
            raw = self.held.get((3, code), info.value)
            return max(-32767, min(32767, round((2 * (raw - info.min) / (info.max - info.min) - 1) * 32767)))

        axes = [value(code) for code in AXES]
        buttons = sum(mask for code, mask in BUTTONS.items() if self.held.get((1, code)))
        buttons |= {-1: 1024, 1: 2048}.get(self.held.get((3, 16)), 0)
        buttons |= {-1: 256, 1: 512}.get(self.held.get((3, 17)), 0)
        left_trigger = value(2) + 32767 if 2 in self.axis_info else 0
        right_trigger = value(5) + 32767 if 5 in self.axis_info else 0
        if self.external_pad is not None:
            axes, buttons, left_trigger, right_trigger = self.external_pad
            axes = list(axes)
        if self.override:
            axis, virtual_buttons, expires = self.override
            if polled < expires:
                axes[2] = round(max(-1, min(1, axis)) * 32767)
                buttons = virtual_buttons
            else:
                self.override = None
        self.sequence = (self.sequence + 1) & 0xffffffff
        packet = struct.pack('<HIdBBHhhhhHHII', 2, self.sequence, polled - self.start_time,
            1, 0, buttons, axes[0], -axes[1], axes[2], -axes[3],
            left_trigger, right_trigger, 0, 0)
        before = channel.get_property('buffered-amount') if self.trial and axes[2] else 0
        channel.emit('send-data', GLib.Bytes.new(packet))
        if self.trial and axes[2]:
            timing = dict(**self.trial, polledAt=polled, sentAt=milliseconds(),
                          queuedBeforeBytes=before, queuedAfterBytes=channel.get_property('buffered-amount'))
            if self.frame_trial is not None:
                self.frame_trial['input'] = timing
            emit('input-timing', **timing)
            self.trial = None
        return GLib.SOURCE_CONTINUE

    def link_track(self, _, pad):
        caps = pad.get_current_caps() or pad.query_caps(None)
        media = caps.get_structure(0).get_string('media')
        if media in self.tracks:
            return
        if media == 'video':
            if sys.platform == 'win32':
                choices = ('d3d11h264dec', 'd3d12h264dec', 'avdec_h264')
            elif sys.platform == 'darwin':
                choices = ('vtdec_hw', 'vtdec', 'avdec_h264')
            else:
                choices = ('vah264dec', 'avdec_h264')
            self.decoder_name = next((name for name in choices if Gst.ElementFactory.find(name)), None)
            if not self.decoder_name:
                emit('error', message='GStreamer H.264 decoder unavailable')
                return
            if self.window_id:
                download = 'vapostproc ! video/x-raw,format=NV12 ! ' if self.decoder_name == 'vah264dec' else ''
                video_output = download + 'videoconvert ! video/x-raw,format=BGRx ! ximagesink'
                options = 'sync=false force-aspect-ratio=true'
            else:
                video_output = 'videoconvert ! autovideosink'
                options = 'sync=false'
            branch = Gst.parse_bin_from_description(
                f'rtph264depay name=depay ! h264parse name=parser ! {self.decoder_name} name=decoder ! '
                f'{video_output} name=video_sink {options}', True)
            self.video_sink = branch.get_by_name('video_sink')
            if self.window_id:
                GstVideo.VideoOverlay.set_window_handle(self.video_sink, self.window_id)
            branch.get_by_name('depay').get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self.receive_rtp)
            self.video_sink.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self.present_frame)
            branch.get_by_name('parser').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.encoded)
            branch.get_by_name('decoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self.decoded)
        elif media == 'audio':
            audio_sink = 'pulsesink' if sys.platform.startswith('linux') and Gst.ElementFactory.find('pulsesink') else 'autoaudiosink'
            branch = Gst.parse_bin_from_description(
                f'rtpopusdepay ! opusdec ! audioconvert ! audioresample ! {audio_sink} sync=true', True)
        else:
            return
        self.tracks.add(media)
        self.pipeline.add(branch)
        pad.link(branch.get_static_pad('sink'))
        branch.sync_state_with_parent()

    def receive_rtp(self, _, info):
        buffer = info.get_buffer()
        self.pending_rtp = (buffer.pts, milliseconds())
        if buffer.pts != Gst.CLOCK_TIME_NONE:
            self.rtp_received[buffer.pts] = self.pending_rtp[1]
            if len(self.rtp_received) > 180:
                self.rtp_received.pop(next(iter(self.rtp_received)))
        return Gst.PadProbeReturn.OK

    def encoded(self, _, info):
        self.encoded_frames += 1
        buffer = info.get_buffer()
        self.encoded_bytes += buffer.get_size()
        received = self.rtp_received.get(buffer.pts)
        if received is not None:
            self.frame_timing[buffer.pts] = {'receivedAt': received}
        if len(self.frame_timing) > 180:
            self.frame_timing.pop(next(iter(self.frame_timing)))
        return Gst.PadProbeReturn.OK

    def decoded(self, pad, probe):
        self.decoded_frames += 1
        timing = self.frame_timing.get(probe.get_buffer().pts)
        if timing is not None:
            timing['decodedAt'] = milliseconds()
        caps = pad.get_current_caps().get_structure(0)
        dimensions = caps.get_value('width'), caps.get_value('height')
        if dimensions != (self.width, self.height):
            self.width, self.height = dimensions
            emit('video', width=self.width, height=self.height, decoder=self.decoder_name)
        return Gst.PadProbeReturn.OK

    def begin_trial(self):
        try:
            import numpy
        except ImportError:
            emit('trial-error', message='Native benchmarking requires NumPy')
            return
        trial = {'numpy': numpy, 'previous': None, 'candidate': None, 'frames': 0,
                 'started': None, 'input': None, 'baselineMoved': False}
        self.frame_trial = trial

        def actuate():
            if self.frame_trial is not trial:
                return GLib.SOURCE_REMOVE
            if trial['baselineMoved'] or trial['frames'] < 4:
                self.finish_trial(trial, error='Hold the camera still and try again')
            else:
                now = milliseconds()
                trial['started'] = now
                self.trial = {'actuatedAt': now}
                self.override = (0.9, 0, now + 230)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(300, actuate)
        GLib.timeout_add(1500, lambda: self.finish_trial(trial, error='No confirmed camera turn'))

    def finish_trial(self, trial, result=None, error=None):
        if self.frame_trial is trial:
            self.frame_trial = None
            if error:
                emit('trial-error', message=error)
            else:
                emit('trial', **result)
        return GLib.SOURCE_REMOVE

    def present_frame(self, pad, probe):
        trial = self.frame_trial
        if trial is None:
            return Gst.PadProbeReturn.OK
        now = milliseconds()
        buffer = probe.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(pad.get_current_caps())
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        np = trial['numpy']
        try:
            image = np.ndarray((info.height, info.width, 4), dtype=np.uint8, buffer=mapped.data,
                               strides=(info.stride[0], 4, 1), offset=info.offset[0])
            y = (np.linspace(0.14, 0.61, 110, endpoint=False) * info.height).astype(int)
            x = (np.linspace(0.10, 0.90, 320, endpoint=False) * info.width).astype(int)
            pixels = image[y[:, None], x, :2].astype(np.int16)
        finally:
            buffer.unmap(mapped)
        trial['frames'] += 1
        previous = trial['previous']
        trial['previous'] = pixels
        if previous is None:
            return Gst.PadProbeReturn.OK
        middle = previous[5:-5:4, 13:-13:3]
        errors = [int(np.abs(middle - pixels[5:-5:4, 13 + shift:320 - 13 + shift:3]).sum())
                  for shift in range(-12, 13)]
        best = min(range(len(errors)), key=errors.__getitem__)
        shifted = errors[12] > 1000 and (errors[12] - errors[best]) / errors[12] >= 0.18
        if trial['started'] is None:
            trial['baselineMoved'] |= shifted
        elif trial['input'] and shifted and best < 12:
            first = trial['candidate']
            if first and now - first['sinkAt'] <= 85:
                event = dict(trial['input'], **first)
                GLib.idle_add(self.finish_trial, trial, event)
            elif self.frame_timing.get(buffer.pts):
                trial['candidate'] = dict(self.frame_timing[buffer.pts], sinkAt=now)
        elif shifted and best > 12:
            trial['candidate'] = None
        return Gst.PadProbeReturn.OK

    def stats(self):
        stats = self.video_sink.get_property('stats') if self.video_sink and self.video_sink.find_property('stats') else None
        emit('stats', time=milliseconds(), width=self.width, height=self.height,
            encodedFrames=self.encoded_frames, encodedBytes=self.encoded_bytes,
            decodedFrames=self.decoded_frames, renderedFrames=stats.get_value('rendered') if stats else 0,
            sinkDropped=stats.get_value('dropped') if stats else 0)

    def bus_message(self, _, message):
        if message.type == Gst.MessageType.ERROR:
            error, _ = message.parse_error()
            emit('error', message=error.message)
            self.loop.quit()

    def run(self):
        def commands():
            for line in sys.stdin:
                try:
                    GLib.idle_add(self.handle, json.loads(line))
                except ValueError:
                    emit('error', message='Invalid command')
            GLib.idle_add(self.loop.quit)

        threading.Thread(target=commands, daemon=True).start()
        try:
            self.loop.run()
        finally:
            self.pipeline.set_state(Gst.State.NULL)
            if self.window:
                self.window.close()
            if self.input_device:
                self.input_device.close()


if __name__ == '__main__':
    try:
        Player().run()
    except (RuntimeError, GLib.Error) as error:
        emit('error', message=str(error))
        sys.exit(1)
