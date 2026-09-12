"""已确认的 USB 身份；端口枚举不打开设备，不发探测命令。"""

BCI_USB_ID = (0x1A86, 0x5722)
RADAR_USB_ID = (0x1A86, 0x7522)  # 立创 S3 的 CH340K，非 S3 原生 USB
ESPRESSIF_VID = 0x303A


def usb_id(port) -> tuple[int | None, int | None]:
    return getattr(port, "vid", None), getattr(port, "pid", None)


def is_bci_port(port) -> bool:
    return (usb_id(port) == BCI_USB_ID
            or "2017" in (getattr(port, "serial_number", None) or "")
            or "2017" in port.device)


def is_radar_text_port(port) -> bool:
    if is_bci_port(port):
        return False
    if usb_id(port) == RADAR_USB_ID:
        return True
    # 部分 WCH 驱动不给 pyserial VID/PID。仅在没有相冲突的身份信息时用节点名兜底。
    vid, pid = usb_id(port)
    return (vid in (None, 0x1A86) and pid in (None, 0x7522)
            and "wchusbserial" in port.device.lower())
