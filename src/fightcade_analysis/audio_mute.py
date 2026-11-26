"""Windows Core Audio (WASAPI) per-process audio muter for background FBNeo capture/ingestion.

Mutes ONLY a specific spawned child process PID via `ISimpleAudioVolume::SetMute(TRUE)` so
background turbo replays stay completely silent without modifying `fcadefbneo.ini` or affecting
your interactive Fightcade matches or AI Training Partner audio.

Pure stdlib Python (`ctypes`) — zero external dependencies.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import sys
import threading
import time


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(guid_str: str) -> _GUID:
    s = guid_str.strip("{}").replace("-", "")
    d1 = int(s[0:8], 16)
    d2 = int(s[8:12], 16)
    d3 = int(s[12:16], 16)
    d4 = (ctypes.c_ubyte * 8)(*[int(s[16 + i * 2 : 18 + i * 2], 16) for i in range(8)])
    return _GUID(d1, d2, d3, d4)


CLSID_MMDeviceEnumerator = _make_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = _make_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioSessionManager2 = _make_guid("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
IID_IAudioSessionControl2 = _make_guid("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
IID_ISimpleAudioVolume = _make_guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")

CLSCTX_ALL = 23
COINIT_MULTITHREADED = 0x0
E_RENDER = 0
E_MULTIMEDIA = 1


def _com_vtable_fn(com_ptr: ctypes.c_void_p, slot: int, restype: object, *argtypes: object):
    vtable = ctypes.cast(com_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return fn_type(vtable[slot])


def _com_release(com_ptr: ctypes.c_void_p | None) -> None:
    if com_ptr and com_ptr.value:
        release = _com_vtable_fn(com_ptr, 2, wintypes.ULONG)
        release(com_ptr)


def mute_process_audio(target_pid: int, *, mute: bool = True) -> bool:
    """Mute (or unmute) all active WASAPI audio sessions owned by `target_pid`.

    Returns True if at least one audio session for `target_pid` was found and updated.
    """
    if sys.platform != "win32" or target_pid <= 0:
        return False

    ole32 = ctypes.windll.ole32
    hr_init = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    should_uninit = hr_init in (0, 1)  # S_OK or S_FALSE

    enum_ptr = ctypes.c_void_p()
    dev_ptr = ctypes.c_void_p()
    mgr_ptr = ctypes.c_void_p()
    sess_enum_ptr = ctypes.c_void_p()
    muted_any = False

    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(CLSID_MMDeviceEnumerator),
            None,
            CLSCTX_ALL,
            ctypes.byref(IID_IMMDeviceEnumerator),
            ctypes.byref(enum_ptr),
        )
        if hr != 0 or not enum_ptr.value:
            return False

        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (slot 4)
        get_default = _com_vtable_fn(
            enum_ptr, 4, ctypes.c_long, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )
        if get_default(enum_ptr, E_RENDER, E_MULTIMEDIA, ctypes.byref(dev_ptr)) != 0 or not dev_ptr.value:
            return False

        # IMMDevice::Activate (slot 3)
        activate = _com_vtable_fn(
            dev_ptr,
            3,
            ctypes.c_long,
            ctypes.POINTER(_GUID),
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        if (
            activate(dev_ptr, ctypes.byref(IID_IAudioSessionManager2), CLSCTX_ALL, None, ctypes.byref(mgr_ptr)) != 0
            or not mgr_ptr.value
        ):
            return False

        # IAudioSessionManager2::GetSessionEnumerator (slot 5)
        get_sess_enum = _com_vtable_fn(mgr_ptr, 5, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))
        if get_sess_enum(mgr_ptr, ctypes.byref(sess_enum_ptr)) != 0 or not sess_enum_ptr.value:
            return False

        # IAudioSessionEnumerator::GetCount (slot 3) & GetSession (slot 4)
        get_count = _com_vtable_fn(sess_enum_ptr, 3, ctypes.c_long, ctypes.POINTER(ctypes.c_int))
        get_session = _com_vtable_fn(
            sess_enum_ptr, 4, ctypes.c_long, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )
        count = ctypes.c_int(0)
        if get_count(sess_enum_ptr, ctypes.byref(count)) != 0:
            return False

        for idx in range(count.value):
            ctrl_ptr = ctypes.c_void_p()
            ctrl2_ptr = ctypes.c_void_p()
            vol_ptr = ctypes.c_void_p()
            try:
                if get_session(sess_enum_ptr, idx, ctypes.byref(ctrl_ptr)) != 0 or not ctrl_ptr.value:
                    continue
                qi = _com_vtable_fn(
                    ctrl_ptr, 0, ctypes.c_long, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)
                )
                if qi(ctrl_ptr, ctypes.byref(IID_IAudioSessionControl2), ctypes.byref(ctrl2_ptr)) != 0 or not ctrl2_ptr.value:
                    continue

                # IAudioSessionControl2::GetProcessId (slot 14)
                get_pid = _com_vtable_fn(ctrl2_ptr, 14, ctypes.c_long, ctypes.POINTER(wintypes.DWORD))
                pid_val = wintypes.DWORD(0)
                if get_pid(ctrl2_ptr, ctypes.byref(pid_val)) != 0 or int(pid_val.value) != int(target_pid):
                    continue

                if qi(ctrl_ptr, ctypes.byref(IID_ISimpleAudioVolume), ctypes.byref(vol_ptr)) != 0 or not vol_ptr.value:
                    continue

                # ISimpleAudioVolume::SetMute (slot 5) & SetMasterVolume (slot 3)
                set_mute = _com_vtable_fn(vol_ptr, 5, ctypes.c_long, wintypes.BOOL, ctypes.c_void_p)
                set_vol = _com_vtable_fn(vol_ptr, 3, ctypes.c_long, ctypes.c_float, ctypes.c_void_p)
                if set_mute(vol_ptr, 1 if mute else 0, None) == 0:
                    if mute:
                        set_vol(vol_ptr, 0.0, None)
                    muted_any = True
            finally:
                _com_release(vol_ptr)
                _com_release(ctrl2_ptr)
                _com_release(ctrl_ptr)

        return muted_any
    except Exception:  # noqa: BLE001
        return False
    finally:
        _com_release(sess_enum_ptr)
        _com_release(mgr_ptr)
        _com_release(dev_ptr)
        _com_release(enum_ptr)
        if should_uninit:
            ole32.CoUninitialize()


def start_mute_watcher(
    target_pid: int,
    stop_event: threading.Event,
    *,
    poll_interval: float = 0.08,
    max_watch_seconds: float = 20.0,
) -> threading.Thread:
    """Spawn a daemon thread that mutes `target_pid` as soon as its audio session registers."""

    def _watch() -> None:
        deadline = time.monotonic() + max_watch_seconds
        muted_once = False
        while not stop_event.is_set() and time.monotonic() < deadline:
            if mute_process_audio(target_pid, mute=True):
                if muted_once:
                    # Confirm twice in case FBNeo re-initializes DirectSound on ROM load
                    break
                muted_once = True
                time.sleep(0.5)
                continue
            time.sleep(poll_interval)

    t = threading.Thread(target=_watch, name=f"mute-pid-{target_pid}", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mute Windows audio for a specific process PID.")
    parser.add_argument("--pid", type=int, required=True, help="Target process ID")
    parser.add_argument("--wait-seconds", type=float, default=0.0, help="Poll up to N seconds until session appears")
    parser.add_argument("--unmute", action="store_true")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + max(0.0, args.wait_seconds)
    while True:
        if mute_process_audio(args.pid, mute=not args.unmute):
            print(f"{'Unmuted' if args.unmute else 'Muted'} audio session for PID {args.pid}")
            return 0
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.1)


if __name__ == "__main__":
    raise SystemExit(main())
