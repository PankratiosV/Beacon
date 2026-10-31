"""Uploading Beacon's sketch to an Arduino Uno over USB, without the Arduino
IDE: the "Install on Arduino" button.

The sketch comes ready-compiled (firmware/*.hex, made by
installer/build_firmware.sh). Uploading talks to the bootloader every Uno
has (Optiboot) with the same STK500 v1 protocol the Arduino IDE's avrdude
uses: reset the board by toggling DTR, sync, check it's an ATmega328P, write
the program 128 bytes at a time, then read it all back and compare.

Old Nano-style clones have the older bootloader at 57600 baud instead of
115200; both speeds are tried. A board without any bootloader can't be
programmed this way (nor from the Arduino IDE over USB)."""

import time

# STK500 v1 bytes (Atmel AVR061)
STK_OK, STK_INSYNC, CRC_EOP = 0x10, 0x14, 0x20
GET_SYNC, ENTER_PROGMODE, LEAVE_PROGMODE = 0x30, 0x50, 0x51
LOAD_ADDRESS, PROG_PAGE, READ_PAGE, READ_SIGN = 0x55, 0x64, 0x74, 0x75

ATMEGA328P = bytes([0x1E, 0x95, 0x0F])
PAGE = 128                      # ATmega328P flash page, bytes
MAX_PROGRAM = 32256             # 32 KB flash minus Optiboot's 512 bytes
BAUDS = (115200, 57600)         # Uno (Optiboot), then old Nano bootloader


class FlashError(RuntimeError):
    """Something went wrong; the message says what to do, in plain words."""


def read_hex(path):
    """The program bytes from an Intel HEX file (as made by the Arduino
    compiler), starting at address 0. Gaps are filled with 0xFF (erased
    flash)."""
    data = {}
    base = 0
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise FlashError(f"{path}: line {n} isn't Intel HEX")
            raw = bytes.fromhex(line[1:])
            if sum(raw) & 0xFF:
                raise FlashError(f"{path}: line {n} has a bad checksum")
            count, addr, kind = raw[0], (raw[1] << 8) | raw[2], raw[3]
            payload = raw[4:4 + count]
            if kind == 0:                                   # data
                for i, b in enumerate(payload):
                    data[base + addr + i] = b
            elif kind == 1:                                 # end of file
                break
            elif kind == 2:                                 # extended segment address
                base = ((payload[0] << 8) | payload[1]) << 4
            elif kind == 4:                                 # extended linear address
                base = ((payload[0] << 8) | payload[1]) << 16
    if not data:
        raise FlashError(f"{path} has no program in it")
    size = max(data) + 1
    if size > MAX_PROGRAM:
        raise FlashError("The program is too big for an Arduino Uno")
    return bytes(data.get(i, 0xFF) for i in range(size))


class _Board:
    """One open connection to the bootloader."""

    def __init__(self, ser):
        self.ser = ser

    def command(self, body, answer_len=0, timeout=1.0):
        """Send one command (body + CRC_EOP) and return the answer bytes
        between INSYNC and OK."""
        self.ser.reset_input_buffer()
        self.ser.write(bytes(body) + bytes([CRC_EOP]))
        self.ser.flush()
        want = answer_len + 2
        got = b""
        end = time.monotonic() + timeout
        while len(got) < want and time.monotonic() < end:
            got += self.ser.read(want - len(got))
        if len(got) < want or got[0] != STK_INSYNC or got[-1] != STK_OK:
            raise FlashError("The Arduino stopped answering during the upload. Unplug it, "
                             "plug it back in and try again.")
        return got[1:-1]

    def sync(self, tries=8):
        """True once the bootloader answers GET_SYNC."""
        for _ in range(tries):
            try:
                self.command([GET_SYNC], timeout=0.25)
                return True
            except FlashError:
                pass
        return False


def _reset(ser):
    """Restart the board into its bootloader, as the Arduino IDE does: DTR
    (and RTS) low then high. The bootloader then listens for about a
    second."""
    ser.dtr = ser.rts = False
    time.sleep(0.25)
    ser.dtr = ser.rts = True
    time.sleep(0.05)
    ser.reset_input_buffer()


def _connect(port):
    """Open the port at whichever speed the bootloader answers. Returns an
    open _Board."""
    import serial               # pyserial is optional in Beacon; only needed here
    for baud in BAUDS:
        try:
            ser = serial.Serial(port, baud, timeout=0.05, write_timeout=2)
        except (serial.SerialException, OSError) as e:
            raise FlashError(f"Can't open {port}. Close the Arduino IDE's Serial Monitor "
                             f"and any other program using it. ({e})") from e
        _reset(ser)
        board = _Board(ser)
        if board.sync():
            return board
        ser.close()
    raise FlashError(f"No Arduino bootloader answered on {port}. Check it's an Arduino Uno "
                     "(or Nano) and the cable carries data, then unplug it, plug it back "
                     "in and try again.")


def upload(port, hex_path, progress=None):
    """Write the program in hex_path to the Uno on `port` and verify it.
    progress(fraction, text) is called as it goes. Raises FlashError."""
    program = read_hex(hex_path)
    program += b"\xff" * (-len(program) % PAGE)     # whole pages (0xFF = erased flash)
    report = progress or (lambda f, t: None)
    report(0.0, "Connecting…")
    board = _connect(port)
    try:
        sig = board.command([READ_SIGN], answer_len=3)
        if sig != ATMEGA328P:
            raise FlashError(f"This board isn't an Arduino Uno (chip {sig.hex(' ')}, expected "
                             "1e 95 0f). Only the Uno and Nano (ATmega328P) are supported.")
        board.command([ENTER_PROGMODE])
        pages = range(0, len(program), PAGE)
        steps = 2 * len(pages)
        for n, addr in enumerate(pages):
            chunk = program[addr:addr + PAGE]
            word = addr // 2                     # the bootloader counts in 16-bit words
            board.command([LOAD_ADDRESS, word & 0xFF, word >> 8])
            board.command([PROG_PAGE, len(chunk) >> 8, len(chunk) & 0xFF, ord("F"), *chunk],
                          timeout=2.0)
            report((n + 1) / steps, "Writing…")
        for n, addr in enumerate(pages):
            chunk = program[addr:addr + PAGE]
            word = addr // 2
            board.command([LOAD_ADDRESS, word & 0xFF, word >> 8])
            back = board.command([READ_PAGE, len(chunk) >> 8, len(chunk) & 0xFF, ord("F")],
                                 answer_len=len(chunk))
            if back != chunk:
                raise FlashError(f"Checking failed at byte {addr}: the Arduino didn't keep "
                                 "what was written. Try again, or try another USB cable.")
            report((len(pages) + n + 1) / steps, "Checking…")
        board.command([LEAVE_PROGMODE])         # the sketch starts now
        report(1.0, "Done")
        return len(program)
    finally:
        try:
            board.ser.close()
        except Exception:
            pass
