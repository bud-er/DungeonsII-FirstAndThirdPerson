"""Packaging boundaries: omit unused installers without changing runtime bytes."""
import hashlib
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from portable_build import extract_runtime, RUNTIME_OMIT_PREFIXES


class RuntimePackaging(unittest.TestCase):
    def files(self):
        return {name: ('original ' + name).encode() for name in (
            'python.exe', 'pythonw.exe', 'python314.dll', 'Lib/os.py',
            'DLLs/_tkinter.pyd', 'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl',
            'LICENSE.txt', 'Lib/site-packages/README.txt',
            'Lib/site-packages/pip/_vendor/distlib/w32.exe',
            'Lib/site-packages/pip-25.2.dist-info/licenses/LICENSE.txt',
            'Lib/ensurepip/_bundled/pip-25.2-py3-none-any.whl')}

    def archive(self, folder, files):
        archive = folder / 'runtime.zip'
        with zipfile.ZipFile(archive, 'w') as package:
            for name, data in files.items():
                package.writestr(name, data)
        return archive

    def test_omits_entire_installers_and_records_exact_upstream_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = self.files()
            runtime, omitted = extract_runtime(self.archive(folder, files), folder / 'runtime')
            excluded = {n: hashlib.sha256(b).hexdigest() for n, b in files.items()
                        if any(n.startswith(p) for p in RUNTIME_OMIT_PREFIXES)}
            self.assertEqual(omitted, excluded)
            actual = {p.relative_to(runtime).as_posix(): p.read_bytes()
                      for p in runtime.rglob('*') if p.is_file()}
            self.assertEqual(actual, {n: b for n, b in files.items() if n not in excluded})

    def test_invalid_omitted_member_still_rejected_before_extraction(self):
        for name in ('Lib/site-packages/pip/../../escape.exe',
                     'Lib/ensurepip/drive:stream', '/outside.exe'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                files = self.files(); files[name] = b'invalid'
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    extract_runtime(self.archive(folder, files), folder / 'runtime')
                self.assertEqual(list((folder / 'runtime').iterdir()), [])

    def test_symlink_in_omitted_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            archive = self.archive(folder, self.files())
            entry = zipfile.ZipInfo('Lib/site-packages/pip/link')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, 'a') as package:
                package.writestr(entry, 'outside')
            with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                extract_runtime(archive, folder / 'runtime')

    def test_changed_upstream_installer_layout_requires_review(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = self.files()
            old = 'Lib/site-packages/pip-25.2.dist-info/licenses/LICENSE.txt'
            files[old.replace('25.2', '26.0')] = files.pop(old)
            with self.assertRaisesRegex(RuntimeError, 'changed package-installation paths'):
                extract_runtime(self.archive(folder, files), folder / 'runtime')

    def test_missing_required_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            files = self.files(); del files['pythonw.exe']
            with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                extract_runtime(self.archive(folder, files), folder / 'runtime')


if __name__ == '__main__':
    unittest.main()
