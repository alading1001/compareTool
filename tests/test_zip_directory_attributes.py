"""ZIP/JAR 空目录的历史属性兼容与拒绝边界。"""
import io
import stat
import zipfile

from archive_workflow_fixtures import WorkflowCase
from diff_engine import DiffEngine
from file_exporter import FileExporter
from test_archive_report import tar_bytes, zip_bytes
from vcs.archive_vcs import ArchiveVCS


def legacy_jar(payload=b'new', *, name='META-INF/maven/', attributes=0xFFFF0010,
               system=3, directory_data=b''):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name)
        info.create_system = system
        info.external_attr = attributes
        archive.writestr(info, directory_data)
        info = zipfile.ZipInfo('META-INF/maven/example/pom.xml')
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        archive.writestr(info, payload)
    return stream.getvalue()


class ZipDirectoryAttributeTests(WorkflowCase):
    def test_legacy_empty_directory_preserves_comparison_and_export(self):
        for suffix in ('.zip', '.jar', '.war', '.ear', '.aar'):
            for excludes in (None, ()):
                with self.subTest(suffix=suffix, sparse=excludes is not None):
                    old, new = self.root / ('old' + suffix), self.root / ('new' + suffix)
                    old.write_bytes(legacy_jar(b'old'))
                    new.write_bytes(legacy_jar(b'new'))
                    vcs = ArchiveVCS(str(old), str(new), extraction_excludes=excludes)
                    try:
                        result = DiffEngine(vcs).generate_diff('old', 'new')
                        self.assertEqual(['META-INF/maven/example/pom.xml'],
                                         [f.file_path for f in result.files])
                        self.assertEqual('0644', result.files[0].new_mode)
                        output = self.root / (suffix[1:] + str(excludes is not None))
                        FileExporter(result, vcs).export(str(output / 'old'), str(output / 'new'))
                        for side, expected in (('old', b'old'), ('new', b'new')):
                            self.assertEqual(expected, (output / side / result.files[0].file_path).read_bytes())
                    finally:
                        vcs.cleanup()

    def test_recursive_worker_keeps_original_delivery_bytes(self):
        old, new = self.root / 'old.zip', self.root / 'new.zip'
        old.write_bytes(zip_bytes({}))
        inner = legacy_jar(b'COMPATIBLE_POM')
        payload = tar_bytes({'lib/ehcache.jar': zip_bytes({'lib/sizeof-agent.jar': inner})})
        new.write_bytes(zip_bytes({'server.tar': payload}))
        task = dict(vcs_type='archive', project_name='Demo',
                    old_version=str(old), new_version=str(new))
        off_app, off, off_out = self.generate(task, False)
        self.assert_success(off_app)
        on_app, on, on_out = self.generate(task, True)
        self.assert_success(on_app)
        self.assertEqual(off.summary, on.summary)
        self.assert_same_delivery(off_out, on_out)
        exported = list((on_out / 'newVersion').rglob('server.tar'))
        self.assertEqual(1, len(exported))
        self.assertEqual(payload, exported[0].read_bytes())
        self.assertIn('COMPATIBLE_POM', (on_out / 'report.html').read_text(encoding='utf-8'))
        self.assert_no_stages(on_out)

    def test_other_unknown_modes_and_real_special_types_remain_rejected(self):
        cases = [dict(name='META-INF/unknown'), dict(attributes=0xFFFF0000),
                 dict(attributes=0xFFFE0010), dict(system=0),
                 dict(directory_data=b'not empty')]
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            cases.append(dict(attributes=((mode | 0o777) << 16) | 0x10))
        for index, options in enumerate(cases):
            with self.subTest(options=options):
                path = self.root / f'unsafe-{index}.jar'
                path.write_bytes(legacy_jar(**options))
                dest = self.root / f'unsafe-{index}'
                dest.mkdir()
                with self.assertRaisesRegex(ValueError, '特殊文件|符号链接'):
                    ArchiveVCS.__new__(ArchiveVCS)._extract_zip(str(path), str(dest))
                self.assertEqual([], list(dest.iterdir()))

    def test_legacy_attributes_do_not_bypass_path_validation(self):
        for index, name in enumerate(('../outside/', '/absolute/', 'C:/absolute/', 'dir:ads/')):
            with self.subTest(name=name):
                path = self.root / f'path-{index}.jar'
                path.write_bytes(legacy_jar(name=name))
                dest = self.root / f'path-{index}'
                dest.mkdir()
                with self.assertRaises(ValueError):
                    ArchiveVCS.__new__(ArchiveVCS)._extract_zip(str(path), str(dest))
                self.assertEqual([], list(dest.iterdir()))

    def test_legacy_directory_local_header_is_still_checked(self):
        data = legacy_jar().replace(b'META-INF/maven/', b'META-INF/broken', 1)
        path, dest = self.root / 'bad-header.jar', self.root / 'bad-header'
        path.write_bytes(data)
        dest.mkdir()
        with self.assertRaises(zipfile.BadZipFile):
            ArchiveVCS.__new__(ArchiveVCS)._extract_zip(str(path), str(dest))
        self.assertEqual([], list(dest.iterdir()))
