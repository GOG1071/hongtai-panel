"""Command-line interface for the HONGTAI LCD panel."""

from __future__ import annotations

import argparse
import errno
import logging
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import serial

from . import sources
from .config import IMAGE_SUFFIXES, PREVIEW_PATH, VIDEO_SUFFIXES, Config
from .protocol import Panel, PanelError, find_panel

log = logging.getLogger("hongtai-panel")


def write_preview(jpeg: bytes) -> None:
    """Publish the current frame for the GUI. Never fatal: preview is cosmetic."""
    try:
        PREVIEW_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREVIEW_PATH.with_suffix(".tmp")
        tmp.write_bytes(jpeg)
        tmp.replace(PREVIEW_PATH)  # atomic, so the GUI never reads a torn file
    except OSError:
        pass


def open_panel(args) -> Panel:
    port = args.port or find_panel()
    if not port:
        raise SystemExit(
            "No panel found. Check `lsusb | grep 33c3:7791` and that /dev/ttyACM* exists.\n"
            "If the device is present but unreadable, run: hongtai-panel install-udev"
        )
    try:
        panel = Panel(port)
    except serial.SerialException as exc:
        # pyserial wraps errno 13 rather than letting PermissionError through.
        if exc.errno == errno.EACCES or "Permission denied" in str(exc):
            raise SystemExit(
                f"Permission denied on {port}.\n\n"
                f"Grant access with:\n"
                f"  {Path(sys.argv[0]).name} install-udev\n"
                f"then unplug and replug the panel."
            ) from None
        if exc.errno == errno.EBUSY or "Device or resource busy" in str(exc):
            raise SystemExit(
                f"{port} is held by another hongtai-panel (usually the service).\n"
                f"Stop it first: systemctl --user stop hongtai-panel"
            ) from None
        raise SystemExit(f"Could not open {port}: {exc}") from None
    info = panel.connect(max_kb_override=getattr(args, "max_kb", 0) or 0)
    log.info(
        "connected: %s  %dx%d  fw=%s  model=%s  %s",
        port, info.width, info.height, info.version or "?", info.model or "?",
        "RGB565" if info.is_spi else f"JPEG<={info.max_frame_kb}KB",
    )
    return panel


def stream(panel: Panel, frames: Iterator[bytes], brightness: int | None,
           preview: bool = False) -> None:
    """Pump frames until interrupted, holding live mode open throughout.

    When `preview` is set, roughly twice a second the frame just sent is also
    written to tmpfs so the GUI can show exactly what is on the panel, in every
    mode, rather than re-rendering and risking drift.
    """
    stopping = False
    last_preview = 0.0

    def on_signal(_sig, _frm):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    panel.start_live()
    if brightness is not None:
        panel.set_brightness(brightness)

    sent = 0
    started = time.monotonic()
    try:
        for frame in frames:
            if stopping:
                break
            panel.send_frame(frame)
            sent += 1
            if preview and not panel.info.is_spi and time.monotonic() - last_preview > 0.5:
                last_preview = time.monotonic()
                write_preview(frame)
            if sent % 300 == 0:
                elapsed = time.monotonic() - started
                log.debug("%d frames, %.1f fps avg", sent, sent / elapsed)
    finally:
        log.info("stopping after %d frames", sent)
        panel.stop_live()


# -- subcommands -------------------------------------------------------------


def cmd_info(args) -> int:
    panel = open_panel(args)
    i = panel.info
    print(f"port          {panel.port_path}")
    print(f"resolution    {i.width}x{i.height}")
    print(f"firmware      {i.version or 'unknown'}")
    print(f"model         {i.model or 'unknown'}")
    print(f"uid           {i.uid or 'unknown'}")
    print(f"rotation      {i.angle}deg")
    print(f"pixel format  {'RGB565 (SPI)' if i.is_spi else 'JPEG'}")
    print(f"frame budget  {i.max_frame_kb} KB @ {i.frame_rate} fps")
    if i.raw:
        print(f"raw           {i.raw}")
    panel.close()
    return 0


def cmd_monitor(args) -> int:
    cfg = Config.load()
    panel = open_panel(args)
    fps = args.fps or min(panel.info.frame_rate, 15)  # dashboards need no more
    log.info("layout=%s at %d fps", args.theme, fps)
    stream(panel, sources.sysmon_frames(panel.info, args.theme, fps, cfg.theme,
                                        args.sample_interval or cfg.sample_interval),
           args.brightness, preview=cfg.preview)
    panel.close()
    return 0


def expand_media(raw_paths: list[str]) -> list[Path]:
    """Resolve files and directories into an ordered playlist."""
    playable = IMAGE_SUFFIXES | VIDEO_SUFFIXES
    paths: list[Path] = []
    for raw in raw_paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            paths += sorted(q for q in p.iterdir() if q.suffix.lower() in playable)
        elif p.exists():
            paths.append(p)
        else:
            raise SystemExit(f"no such file: {p}")
    if not paths:
        raise SystemExit("no playable images or videos found")
    return paths


def cmd_play(args) -> int:
    """Display images, videos, GIFs, or any mix — optionally with stats on top."""
    cfg = Config.load()
    paths = expand_media(args.path)
    panel = open_panel(args)
    fps = args.fps or panel.info.frame_rate
    theme = cfg.theme
    if args.scrim is not None:
        theme.scrim = args.scrim
    stream(
        panel,
        sources.display_frames(
            panel.info, paths, args.overlay, args.theme or cfg.layout, fps, theme,
            cfg.sample_interval, args.interval, args.fit, not args.no_loop, args.quality,
        ),
        args.brightness,
        preview=cfg.preview,
    )
    panel.close()
    return 0


def cmd_mirror(args) -> int:
    panel = open_panel(args)
    fps = args.fps or panel.info.frame_rate
    log.info("requesting screen-share permission from the compositor")
    stream(panel, sources.mirror_frames(panel.info, fps, args.fit), args.brightness)
    panel.close()
    return 0


def cmd_run(args) -> int:
    """Drive the panel from the saved config. This is what the service runs."""
    cfg = Config.load()
    args.port = args.port or cfg.port or None
    args.max_kb = args.max_kb or cfg.max_kb
    panel = open_panel(args)
    info = panel.info
    brightness = args.brightness if args.brightness is not None else cfg.brightness

    if cfg.mode == "mirror":
        frames = sources.mirror_frames(info, cfg.fps, cfg.fit)
        log.info("mode=mirror fps=%d", cfg.fps)
    elif cfg.mode == "display":
        paths = [Path(p) for p in cfg.media_paths if Path(p).exists()]
        missing = len(cfg.media_paths) - len(paths)
        if missing:
            log.warning("skipping %d background file(s) that no longer exist", missing)
        if not paths and not cfg.overlay:
            raise SystemExit(
                "nothing to display: no background files and the stats overlay is off"
            )
        frames = sources.display_frames(
            info, paths, cfg.overlay, cfg.layout, cfg.fps, cfg.theme,
            cfg.sample_interval, cfg.image_interval, cfg.fit, cfg.loop,
        )
        log.info("mode=display background=%s overlay=%s layout=%s fps=%d sample=%.1fs",
                 f"{len(paths)} file(s)" if paths else "none",
                 "on" if cfg.overlay else "off", cfg.layout, cfg.fps, cfg.sample_interval)
    else:
        raise SystemExit(f"unknown mode: {cfg.mode}")
    stream(panel, frames, brightness, preview=cfg.preview)
    panel.close()
    return 0


def cmd_gui(args) -> int:
    from .gui import run_gui

    return run_gui()


def cmd_brightness(args) -> int:
    panel = open_panel(args)
    panel.set_brightness(args.level)
    log.info("brightness set to %d", args.level)
    panel._serial.close()
    return 0


def cmd_clear(args) -> int:
    panel = open_panel(args)
    panel.clear()
    log.info("panel cleared")
    panel._serial.close()
    return 0


UDEV_RULE = (
    '# HONGTAI / LOVINGCOOL USB serial LCD panel\n'
    'SUBSYSTEM=="tty", ATTRS{idVendor}=="33c3", ATTRS{idProduct}=="7791", '
    'MODE="0660", GROUP="dialout", TAG+="uaccess", SYMLINK+="hongtai-panel"\n'
)
# 70-, not 99-: 73-seat-late.rules turns the uaccess tag into an ACL, so a rule
# sorted after it sets the tag too late and the user gets "Permission denied".
UDEV_PATH = Path("/etc/udev/rules.d/70-hongtai-panel.rules")
LEGACY_UDEV_PATH = Path("/etc/udev/rules.d/99-hongtai-panel.rules")


def root_prefix() -> list[str]:
    """argv prefix that runs a command as root.

    sudo when there is a terminal to ask on, pkexec otherwise: the GUI is
    launched from the app menu with no tty, where sudo has nowhere to prompt.
    """
    import os
    import shutil

    if os.geteuid() == 0:
        return []
    if sys.stdin is not None and sys.stdin.isatty():
        return ["sudo"]
    return ["pkexec"] if shutil.which("pkexec") else ["sudo"]


def cmd_install_udev(args) -> int:
    """Grant the logged-in user access without touching group membership.

    TAG+="uaccess" makes systemd-logind hand the device to whoever is at the
    seat, which survives reboots and needs no re-login.

    Escalates the work rather than expecting to be run as root: a --user pip
    install is not importable from root's interpreter, so
    `sudo hongtai-panel install-udev` would fail before reaching this code.
    Write and reload go in one shell so there is a single password prompt.
    """
    import os
    import subprocess

    script = (
        "set -e\n"
        f"cat > {UDEV_PATH} <<'EOF'\n{UDEV_RULE}EOF\n"
        f"rm -f {LEGACY_UDEV_PATH}\n"
        "udevadm control --reload-rules\n"
        "udevadm trigger --subsystem-match=tty\n"
    )
    print(f"Writing {UDEV_PATH} (needs root; you may be prompted for your password)")
    result = subprocess.run(root_prefix() + ["/bin/sh", "-c", script])
    if result.returncode != 0:
        print("\nCould not write the rule. Do it manually:\n")
        print(f"  sudo tee {UDEV_PATH} <<'EOF'\n{UDEV_RULE}EOF")
        print("  sudo udevadm control --reload-rules && sudo udevadm trigger")
        return 1

    print(f"Installed {UDEV_PATH}")
    if Path("/dev/ttyACM0").exists() and not os.access("/dev/ttyACM0", os.R_OK | os.W_OK):
        print("Now unplug and replug the panel for the rule to take effect.")
    else:
        print("Panel is accessible. Try: hongtai-panel info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hongtai-panel",
        description="Drive HONGTAI/LOVINGCOOL USB serial LCD panels (33c3:7791) on Linux.",
    )
    p.add_argument("--port", help="serial device (default: autodetect by USB id)")
    p.add_argument("--brightness", type=int, metavar="0-100", help="set brightness on start")
    p.add_argument("--max-kb", type=int, default=0,
                   help="override the per-frame JPEG budget (lower this if frames tear)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("run", help="drive the panel from the saved config (used by the service)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("gui", help="open the graphical control panel")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("info", help="show panel geometry and capabilities")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("monitor", help="display live system stats")
    s.add_argument("--theme", choices=sorted(sources.render.LAYOUTS), default="gauges")
    s.add_argument("--fps", type=int, help="refresh rate (default: 15)")
    s.add_argument("--sample-interval", type=float, default=0.0,
                   help="seconds between telemetry samples (default: 1.0)")
    s.set_defaults(func=cmd_monitor)

    for name, help_text in (
        ("play", "display images, videos, GIFs, or a mixed playlist"),
        ("show", "alias for play"),
    ):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("path", nargs="+", help="files or directories, played in order")
        s.add_argument("--interval", type=float, default=10.0,
                       help="seconds to hold each still image")
        s.add_argument("--fps", type=int, help="frame rate (default: panel maximum)")
        s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
        s.add_argument("--quality", type=int, default=6, help="ffmpeg -q:v, 2=best 31=worst")
        s.add_argument("--no-loop", action="store_true", help="stop after one pass")
        s.add_argument("--overlay", action="store_true",
                       help="draw the system-stats overlay on top of the background")
        s.add_argument("--theme", choices=sorted(sources.render.LAYOUTS),
                       help="overlay layout (default: whatever the config uses)")
        s.add_argument("--scrim", type=float, metavar="0.0-1.0",
                       help="how much to dim the background under the overlay")
        s.set_defaults(func=cmd_play)

    s = sub.add_parser("mirror", help="mirror a monitor or window to the panel")
    s.add_argument("--fps", type=int, help="frame rate (default: panel maximum)")
    s.add_argument("--fit", choices=["cover", "contain", "stretch"], default="cover")
    s.set_defaults(func=cmd_mirror)

    s = sub.add_parser("brightness", help="set backlight level and exit")
    s.add_argument("level", type=int, metavar="0-100")
    s.set_defaults(func=cmd_brightness)

    s = sub.add_parser("clear", help="blank the panel and exit")
    s.set_defaults(func=cmd_clear)

    s = sub.add_parser("install-udev", help="install the udev rule granting device access")
    s.set_defaults(func=cmd_install_udev)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Pillow logs every PNG chunk at DEBUG, which drowns out our own output.
    logging.getLogger("PIL").setLevel(logging.INFO)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (PanelError, RuntimeError, ValueError) as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
