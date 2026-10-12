"""Fake pyserial for the harness tests. Records to $FAKE_SERIAL_LOG (JSON lines: import / open / write) and answers
EC console commands from $FAKE_SERIAL_REPLIES (JSON {"<command word>": "<reply text>"}), echoing the command first
like the EC console does; FAKE_SERIAL_HANG=1 makes opening the port hang; with FAKE_SERIAL_BOOT_MENU set, an EC
`powerbtn` appends that file (a firmware menu draw) to FAKE_SERIAL_BOOT_LOG, and with FAKE_SERIAL_EDITOR set, an `e`
sent to the AP appends that file (the menu's line editor drawing the entry's options) to it as well. Never touches a
real port."""
import json, os, time

_LOG = os.environ.get("FAKE_SERIAL_LOG")


def _note(**kw):
    if _LOG:
        with open(_LOG, "a") as f:
            f.write(json.dumps(kw) + "\n")


_note(event="import")


class Serial:
    def __init__(self, dev, baud, timeout=None, exclusive=None):
        self.dev, self.timeout, self.pending = dev, timeout or 0.05, b""
        _note(event="open", dev=dev)
        if os.environ.get("FAKE_SERIAL_HANG"):
            time.sleep(3600)                     # a wedged CCD port

    def reset_input_buffer(self):
        pass

    def write(self, data):
        _note(event="write", dev=self.dev, hex=data.hex())
        words = data.decode("ascii", "replace").strip("\r").split()
        if words and words[0] == "powerbtn" and os.environ.get("FAKE_SERIAL_BOOT_MENU"):
            # the AP boots and the firmware draws the systemd-boot menu on the AP UART (FAKE_SERIAL_BOOT_LOG)
            with open(os.environ["FAKE_SERIAL_BOOT_MENU"], "rb") as src, open(os.environ["FAKE_SERIAL_BOOT_LOG"], "ab") as dst:
                dst.write(src.read())
        if data == b"e" and os.environ.get("FAKE_SERIAL_EDITOR"):
            # "editor yes": the menu opens the line editor on the status row
            with open(os.environ["FAKE_SERIAL_EDITOR"], "rb") as src, open(os.environ["FAKE_SERIAL_BOOT_LOG"], "ab") as dst:
                dst.write(src.read())
        replies = json.loads(os.environ.get("FAKE_SERIAL_REPLIES", "{}"))
        if words and words[0] in replies:
            self.pending += ("> " + " ".join(words) + "\r\n" + replies[words[0]] + "\r\n").encode()
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        out, self.pending = self.pending[:n], self.pending[n:]
        if not out:
            time.sleep(min(self.timeout, 0.05))
        return out
