import struct
import unittest

import civ5_host_startup as startup


def executable():
    data = bytearray(4096)
    struct.pack_into('<8I', data, 0, 0xfeedfacf, 0x1000007, 3, 2, 1, 152, 0, 0)
    struct.pack_into('<II16sQQQQIIII', data, 32, 0x19, 152, b'__TEXT', 0x100000000,
                     4096, 0, 4096, 7, 5, 1, 0)
    struct.pack_into('<16s16sQQIIIIIIII', data, 104, b'__text', b'__TEXT',
                     0x100000400, 64, 1024, 0, 0, 0, 0, 0, 0, 0)
    data[1024:1088] = b'X' * 64
    return bytes(data)


class LoadCommandTests(unittest.TestCase):
    def test_fixed_dependency_does_not_move_or_change_code(self):
        original = executable()
        patched = startup.add_load_command(original)
        self.assertEqual(len(patched), len(original))
        self.assertEqual(patched[1024:], original[1024:])
        self.assertEqual(patched[32:184], original[32:184])
        self.assertEqual(struct.unpack_from('<I', patched, 16)[0], 2)
        self.assertIn(startup.LOAD_PATH.encode(), patched[184:1024])
        with self.assertRaisesRegex(startup.HostStartupError, 'already exists'):
            startup.add_load_command(patched)

    def test_rejects_truncation_wrong_cpu_non_executable_and_bad_tables(self):
        for offset, value in [(0, 0), (4, 0x100000c), (12, 6), (16, 129),
                              (20, 99999), (36, 7), (96, 2)]:
            with self.subTest(offset=offset):
                data = bytearray(executable())
                struct.pack_into('<I', data, offset, value)
                with self.assertRaises(startup.HostStartupError):
                    startup.add_load_command(data)
        with self.assertRaises(startup.HostStartupError):
            startup.add_load_command(b'short')

    def test_rejects_live_padding_and_insufficient_padding(self):
        data = bytearray(executable())
        data[184] = 1
        with self.assertRaisesRegex(startup.HostStartupError, 'padding'):
            startup.add_load_command(data)
        data = bytearray(executable())
        struct.pack_into('<I', data, 152, 200)
        with self.assertRaisesRegex(startup.HostStartupError, 'padding'):
            startup.add_load_command(data)
