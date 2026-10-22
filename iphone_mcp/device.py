"""Device layer: talks to the phone's lockdown services over USB (or Wi-Fi
once paired) via pymobiledevice3. Needs the phone plugged in once and
"Trust This Computer" accepted. No Developer Mode, no Xcode.

Read-only on purpose: storage numbers, app sizes, device facts. Anything that
deletes data goes through the phone's own UI so the user sees it happen.
"""

from __future__ import annotations

from typing import Any

from pymobiledevice3 import usbmux
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.installation_proxy import InstallationProxyService

MODELS = {
    "iPhone14,4": "iPhone 13 mini", "iPhone14,5": "iPhone 13", "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max", "iPhone14,7": "iPhone 14", "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro", "iPhone15,3": "iPhone 14 Pro Max", "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus", "iPhone16,1": "iPhone 15 Pro", "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone13,2": "iPhone 12", "iPhone13,1": "iPhone 12 mini", "iPhone12,1": "iPhone 11",
}


class DeviceUnavailable(RuntimeError):
    pass


def gb(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e3:.0f} KB"


async def devices() -> list[Any]:
    try:
        return await usbmux.list_devices()
    except Exception as e:  # usbmuxd missing or refusing
        raise DeviceUnavailable(f"usbmuxd not reachable: {e}") from e


async def _lockdown():
    devs = await devices()
    if not devs:
        raise DeviceUnavailable(
            "No iPhone visible over USB or Wi-Fi sync. Plug the phone into the Mac with a cable, unlock it, "
            "and tap 'Trust' (the user enters their own passcode). After that, Finder > the iPhone > "
            "'Show this iPhone when on Wi-Fi' lets this work without the cable.")
    usb = [d for d in devs if d.connection_type == "USB"]
    dev = (usb or devs)[0]
    try:
        return await create_using_usbmux(serial=dev.serial, autopair=True, pair_timeout=60)
    except Exception as e:
        raise DeviceUnavailable(
            f"Could not open a lockdown session ({type(e).__name__}: {e}). If the phone shows "
            "'Trust This Computer?', the user should tap Trust and enter their passcode.") from e


async def info() -> dict[str, Any]:
    ld = await _lockdown()
    try:
        allv = await ld.get_value()
        battery = await _safe(ld.get_value(domain="com.apple.mobile.battery"))
        disk = await _safe(ld.get_value(domain="com.apple.disk_usage"))
        return {"values": allv or {}, "battery": battery or {}, "disk": disk or {}}
    finally:
        await ld.close()


async def apps(application_type: str = "User", sizes: bool = True) -> list[dict[str, Any]]:
    ld = await _lockdown()
    try:
        svc = InstallationProxyService(lockdown=ld)
        found = await svc.get_apps(application_type=application_type, calculate_sizes=sizes)
    finally:
        await ld.close()
    out = []
    for bid, a in found.items():
        static = a.get("StaticDiskUsage") or 0
        dynamic = a.get("DynamicDiskUsage") or 0
        out.append({
            "bundle_id": bid,
            "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or bid,
            "version": a.get("CFBundleShortVersionString", ""),
            "app_bytes": static,
            "data_bytes": dynamic,
            "total_bytes": static + dynamic,
        })
    return out


async def _safe(coro):
    try:
        return await coro
    except Exception:
        return None


def model_name(product_type: str | None) -> str:
    return MODELS.get(product_type or "", product_type or "?")
