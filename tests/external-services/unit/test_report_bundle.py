import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('bundle',(Path(__file__).resolve().parents[1] / 'report-bundle.py'))
bundle=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundle)

class Bundle(unittest.TestCase):
    def source(self,root):
        root.mkdir(mode=0o700)
        values={'summary.json':json.dumps({'mock_only':True,'phases':[]}),
                'cleanup.json':json.dumps({'complete':True,'errors':[]}),
                'observe.jsonl':'{"utc":"2026-01-01T00:00:00Z"}\n'}
        for name,value in values.items():
            (root/name).write_text(value);(root/name).chmod(0o600)
        (root/'alice.key').write_text('SECRET')
        (root/'gateway-original.json').write_text('SECRET')
        return values

    def test_round_trip_excludes_keys_and_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);values=self.source(root/'source');stream=io.BytesIO()
            bundle.export(root/'source',stream)
            self.assertNotIn(b'SECRET',stream.getvalue())
            stream.seek(0);bundle.unpack(stream,root/'result')
            self.assertEqual(set(p.name for p in (root/'result').iterdir()),set(values))
            for name,value in values.items():
                self.assertEqual((root/'result'/name).read_text(),value)
                self.assertEqual((root/'result'/name).stat().st_mode&0o777,0o600)
            self.assertEqual((root/'result').stat().st_mode&0o777,0o700)

    def test_export_rejects_links_public_files_missing_cleanup_and_nonmock(self):
        for mode in ('link','hardlink','public','missing','real','directory'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);self.source(root/'source');p=root/'source'/'summary.json'
                if mode=='link':p.rename(root/'target');p.symlink_to(root/'target')
                if mode=='hardlink':os.link(p,root/'another')
                if mode=='public':p.chmod(0o644)
                if mode=='missing':(root/'source'/'cleanup.json').unlink()
                if mode=='real':p.write_text('{"mock_only":false}')
                if mode=='directory':(root/'source').chmod(0o755)
                output=io.BytesIO()
                with self.assertRaises((ValueError,OSError)):bundle.export(root/'source',output)
                self.assertEqual(output.getvalue(),b'')

    def test_import_refuses_unsafe_duplicate_missing_extra_and_large_members(self):
        for mode in ('traversal','link','extra','duplicate','missing','large'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);stream=io.BytesIO()
                names=['summary.json','cleanup.json','observe.jsonl']
                if mode=='traversal':names[0]='../summary.json'
                if mode=='extra':names.append('alice.key')
                if mode=='duplicate':names.append('summary.json')
                if mode=='missing':names.pop()
                with tarfile.open(fileobj=stream,mode='w') as t:
                    for name in names:
                        info=tarfile.TarInfo(name);data=b'{}'
                        if mode=='link':info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';data=b''
                        info.size=bundle.MAX_BYTES+1 if mode=='large' else len(data)
                        if mode=='large':
                            # Header alone is enough: validation must reject before reading payload.
                            t.fileobj.write(info.tobuf());break
                        t.addfile(info,io.BytesIO(data))
                stream.seek(0)
                with self.assertRaises((ValueError,tarfile.TarError)):bundle.unpack(stream,root/'result')
                self.assertFalse((root/'result').exists())

    def test_archive_reader_bounds_metadata_as_well_as_file_payload(self):
        source=bundle.BoundedReader(io.BytesIO(b'12345'),4)
        self.assertEqual(source.read(3),b'123')
        with self.assertRaises(ValueError):source.read(500000000)
        with self.assertRaises(ValueError):bundle.BoundedReader(io.BytesIO(b'12345'),4).read()

    def test_failed_cleanup_is_preserved_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.source(root/'source')
            (root/'source'/'cleanup.json').write_text('{"complete":false,"errors":["restore:RuntimeError"]}')
            stream=io.BytesIO();bundle.export(root/'source',stream);stream.seek(0)
            bundle.unpack(stream,root/'result')
            self.assertFalse(json.loads((root/'result'/'cleanup.json').read_text())['complete'])

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.source(root/'source');stream=io.BytesIO();bundle.export(root/'source',stream)
            stream.seek(0);target=root/'result';target.mkdir();(target/'keep').write_text('keep')
            with self.assertRaises(FileExistsError):bundle.unpack(stream,target)
            self.assertEqual((target/'keep').read_text(),'keep')

if __name__=='__main__':unittest.main()
