import errno
import importlib.util
import os
import select
import stat
import sys
import tempfile
import termios
import types
import unittest
from pathlib import Path
from unittest import mock

from helpers import HARNESS

spec = importlib.util.spec_from_file_location('kano_ser', HARNESS / 'kano-ser.py')
ks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ks)


class PureTests(unittest.TestCase):
    def test_new_cli_rejects_bad_arity_and_hex_before_open(self):
        invalid = [[], [''], ['0'], ['zz'], ['00 01'], ['00' * 9], ['01', '02']]
        with mock.patch.object(ks, 'load_env'), mock.patch.dict(os.environ, {'KANO_AP_TTY': '/fake'}):
            for args in invalid:
                with self.subTest(args=args), mock.patch.object(os, 'open') as opened:
                    with self.assertRaises(SystemExit) as exc:
                        ks.main(['ap-keys-preserve-rx', *args])
                    self.assertEqual(exc.exception.code, 2)
                    opened.assert_not_called()

    def test_missing_tty_is_refused_before_port_action(self):
        with mock.patch.object(ks, 'load_env'), mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(os, 'open') as opened:
            with self.assertRaises(SystemExit) as exc:
                ks.main(['ap-keys-preserve-rx', '1b'])
            self.assertEqual(exc.exception.code, 2)
            opened.assert_not_called()

    def test_legacy_ap_keys_still_uses_existing_transport(self):
        port = mock.Mock()
        with mock.patch.object(ks, 'load_env'), mock.patch.dict(os.environ, {'KANO_AP_TTY': '/fake'}), \
                mock.patch.object(ks, 'port', return_value=port) as legacy, \
                mock.patch.object(ks, 'ap_keys_preserve_rx') as new:
            ks.main(['ap-keys', '1b5b41'])
            legacy.assert_called_once_with('/fake')
            port.write.assert_called_once_with(b'\x1b[A')
            new.assert_not_called()

    def test_unsupported_platform_and_empty_data_refused_before_open(self):
        with mock.patch.object(ks.sys, 'platform', 'darwin'), mock.patch.object(os, 'open') as opened:
            with self.assertRaises(ValueError): ks.ap_keys_preserve_rx('/fake', b'a')
            opened.assert_not_called()
        with mock.patch.object(ks.sys, 'platform', 'linux'), mock.patch.object(os, 'open') as opened:
            for data in (b'', b'x' * 9, 'ab'):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    ks.ap_keys_preserve_rx('/fake', data)
            opened.assert_not_called()

    def test_regular_file_refused_before_open(self):
        with tempfile.NamedTemporaryFile() as f, mock.patch.object(ks.sys, 'platform', 'linux'), \
                mock.patch.object(os, 'open') as opened:
            with self.assertRaises(ValueError): ks.ap_keys_preserve_rx(f.name, b'x')
            opened.assert_not_called()


class MockedWriteTests(unittest.TestCase):
    def setUp(self):
        self.a = types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_dev=4, st_ino=5, st_rdev=6)
        self.attrs = [0, 0, termios.CS8 | termios.CLOCAL | termios.CREAD, 0,
                      termios.B115200, termios.B115200, []]

    def run_case(self, *, write_result=None, write_error=None, attrs=None, stats=None):
        fake_write = mock.Mock(side_effect=write_error, return_value=write_result)
        with mock.patch.object(ks.sys, 'platform', 'linux'), \
                mock.patch.object(os, 'stat', side_effect=stats or [self.a, self.a]), \
                mock.patch.object(os, 'fstat', return_value=self.a), \
                mock.patch.object(os, 'open', return_value=17) as opened, \
                mock.patch.object(os, 'close') as closed, \
                mock.patch.object(os, 'write', fake_write), \
                mock.patch.object(termios, 'tcgetattr', return_value=attrs or self.attrs):
            try:
                ks.ap_keys_preserve_rx('/fake', b'\x1b[A')
                caught = None
            except Exception as exc:
                caught = exc
            opened.assert_called_once_with('/fake', os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
            closed.assert_called_once_with(17)
        return caught, fake_write

    def test_one_complete_write(self):
        exc, write = self.run_case(write_result=3)
        self.assertIsNone(exc)
        write.assert_called_once_with(17, b'\x1b[A')

    def test_short_or_eagain_is_error_without_retry_and_fd_is_closed(self):
        for kwargs in ({'write_result': 1}, {'write_error': BlockingIOError(errno.EAGAIN, 'again')}):
            with self.subTest(kwargs=kwargs):
                exc, write = self.run_case(**kwargs)
                self.assertIsInstance(exc, OSError)
                write.assert_called_once_with(17, b'\x1b[A')

    def test_9600_requires_exact_google_driver_and_module(self):
        attrs = self.attrs.copy()
        attrs[4] = attrs[5] = termios.B9600
        node = Path('/sys/dev/char') / f'{os.major(self.a.st_rdev)}:{os.minor(self.a.st_rdev)}' / 'device/driver'
        driver = Path('/sys/bus/usb-serial/drivers/google')
        module = Path('/sys/module/usb_serial_simple')

        def resolver(driver_result, module_result):
            def resolve(path, *, strict):
                self.assertTrue(strict)
                if path == node:
                    if isinstance(driver_result, Exception): raise driver_result
                    return driver_result
                if path == driver / 'module':
                    if isinstance(module_result, Exception): raise module_result
                    return module_result
                self.fail(f'unexpected sysfs path {path}')
            return resolve

        cases = [
            (driver, module, True),
            (Path('/sys/bus/usb-serial/drivers/other'), module, False),
            (driver, Path('/sys/module/other'), False),
            (FileNotFoundError('driver'), module, False),
            (driver, FileNotFoundError('module'), False),
        ]
        for seen_driver, seen_module, accepted in cases:
            with self.subTest(driver=seen_driver, module=seen_module), mock.patch.object(
                    Path, 'resolve', autospec=True, side_effect=resolver(seen_driver, seen_module)):
                exc, write = self.run_case(write_result=3, attrs=attrs)
                if accepted:
                    self.assertIsNone(exc)
                    write.assert_called_once_with(17, b'\x1b[A')
                else:
                    self.assertIsInstance(exc, ValueError)
                    write.assert_not_called()

        # Matching driver does not relax the other tty checks.
        bad_raw = attrs.copy(); bad_raw[3] |= termios.ICANON
        with mock.patch.object(Path, 'resolve', autospec=True, side_effect=resolver(driver, module)):
            exc, write = self.run_case(write_result=3, attrs=bad_raw)
            self.assertIsInstance(exc, ValueError)
            write.assert_not_called()

    def test_mixed_speeds_refused_and_115200_needs_no_driver_exception(self):
        for first, second in ((termios.B9600, termios.B115200),
                              (termios.B115200, termios.B9600)):
            attrs = self.attrs.copy(); attrs[4] = first; attrs[5] = second
            with self.subTest(first=first, second=second), mock.patch.object(Path, 'resolve') as resolve:
                exc, write = self.run_case(write_result=3, attrs=attrs)
                self.assertIsInstance(exc, ValueError)
                write.assert_not_called()
                resolve.assert_not_called()
        with mock.patch.object(Path, 'resolve') as resolve:
            exc, write = self.run_case(write_result=3)
            self.assertIsNone(exc)
            write.assert_called_once()
            resolve.assert_not_called()

    def test_bad_modes_and_replaced_device_refused_before_write(self):
        modes = []
        for index, value in [(0, termios.ICRNL), (1, termios.OPOST), (2, termios.PARENB),
                             (3, termios.ICANON), (4, termios.B9600), (5, termios.B9600)]:
            a = self.attrs.copy(); a[index] |= value if index < 4 else 0
            if index >= 4: a[index] = value
            modes.append(a)
        for attrs in modes:
            with self.subTest(attrs=attrs):
                with mock.patch.object(Path, 'resolve', side_effect=FileNotFoundError('no driver')):
                    exc, write = self.run_case(attrs=attrs)
                self.assertIsInstance(exc, ValueError)
                write.assert_not_called()
        replacement = types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_dev=4, st_ino=6, st_rdev=6)
        exc, write = self.run_case(stats=[self.a, replacement])
        self.assertIsInstance(exc, ValueError)
        write.assert_not_called()


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PTY behavior')
class LinuxPtyTests(unittest.TestCase):
    def setUp(self):
        import pty
        import tty
        self.master, self.slave = pty.openpty()
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)
        tty.setraw(self.slave)
        a = termios.tcgetattr(self.slave)
        # setraw/cfmakeraw leaves some echo subflags and software flow-control
        # bits untouched. Configure all flags required by the writer explicitly.
        def bits(*names):
            mask = 0
            for name in names:
                mask |= getattr(termios, name, 0)
            return mask
        a[0] &= ~bits('IGNBRK', 'BRKINT', 'PARMRK', 'ISTRIP', 'INLCR', 'IGNCR', 'ICRNL',
                      'IXON', 'IXOFF', 'IXANY', 'INPCK', 'IUCLC')
        a[1] &= ~termios.OPOST
        a[2] &= ~bits('CSIZE', 'PARENB', 'PARODD', 'CSTOPB', 'CRTSCTS', 'CMSPAR')
        a[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
        a[3] &= ~bits('ECHO', 'ECHOE', 'ECHOK', 'ECHONL', 'ECHOCTL', 'ECHOKE',
                      'ICANON', 'ISIG', 'IEXTEN')
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(self.slave, termios.TCSANOW, a)
        applied = termios.tcgetattr(self.slave)
        self.assertEqual(applied[3] & bits('ECHOCTL', 'ECHOKE'), 0,
                         'PTY must clear the echo subflags required by the writer')
        self.path = os.ttyname(self.slave)

    def ready(self, fd):
        return bool(select.select([fd], [], [], 1)[0])

    def test_queued_rx_is_preserved_and_tx_delivered(self):
        os.write(self.master, b'queued-ap-rx')
        self.assertTrue(self.ready(self.slave))
        ks.ap_keys_preserve_rx(self.path, b'\x1b[A')
        self.assertTrue(self.ready(self.master))
        self.assertEqual(os.read(self.master, 3), b'\x1b[A')
        self.assertEqual(os.read(self.slave, 12), b'queued-ap-rx')

    def test_bad_configuration_sends_no_data(self):
        a = termios.tcgetattr(self.slave); a[3] |= termios.ICANON
        termios.tcsetattr(self.slave, termios.TCSANOW, a)
        with self.assertRaises(ValueError): ks.ap_keys_preserve_rx(self.path, b'\x1b[A')
        self.assertFalse(select.select([self.master], [], [], 0.1)[0])

    def test_no_input_or_invalid_hex_sends_no_data(self):
        with mock.patch.object(ks, 'load_env'), mock.patch.dict(os.environ, {'KANO_AP_TTY': self.path}):
            for value in ('', '0', 'xx', '00' * 9):
                with self.assertRaises(SystemExit): ks.main(['ap-keys-preserve-rx', value])
        self.assertFalse(select.select([self.master], [], [], 0.1)[0])


if __name__ == '__main__':
    unittest.main()
