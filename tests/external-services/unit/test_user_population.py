"""Durable records stop uncertain creates and preserve immutable IDs on resume."""
import importlib.util
from pathlib import Path
import tempfile
import os
import fcntl
import unittest
import uuid
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('population',(Path(__file__).resolve().parents[1] / 'user-population.py'))
pop=importlib.util.module_from_spec(spec);spec.loader.exec_module(pop)


class Population(unittest.TestCase):
    def test_success_and_resume_do_not_recreate_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';pop.initialize(root,3);users={}
            def create(profile):
                identity=str(uuid.uuid4());users[identity]={'id':identity,**profile};return identity
            create=Mock(side_effect=create)
            result=pop.run(root,create,users.__getitem__)
            self.assertEqual(result,{'issuer_users_verified':3,'pricetag_provisioned':False})
            self.assertEqual(create.call_count,3)
            pop.run(root,create,users.__getitem__);self.assertEqual(create.call_count,3)
            for path in root.glob('*.json'):self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(len({u['credentials'][0]['value'] for u in users.values()}),3)

    def test_uncertain_create_is_not_retried_and_errors_do_not_print_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';pop.initialize(root,2)
            create=Mock(side_effect=ValueError('credential-sentinel'))
            with self.assertRaises(ValueError) as error:pop.run(root,create,Mock())
            self.assertNotIn('credential-sentinel',str(error.exception))
            record=pop.load(root,'00001.json');self.assertEqual(record['state'],'creating')
            with self.assertRaisesRegex(ValueError,'uncertain'):pop.run(root,create,Mock())
            self.assertEqual(create.call_count,1);self.assertFalse((root/'00002.json').exists())

    def test_created_identity_is_rechecked_after_failed_read_without_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';pop.initialize(root,1);identity=str(uuid.uuid4())
            create=Mock(return_value=identity)
            with self.assertRaisesRegex(ValueError,'did not verify'):pop.run(root,create,Mock(return_value={'id':identity,'username':'foreign'}))
            record=pop.load(root,'00001.json');self.assertEqual(record['user_id'],identity)
            pop.run(root,create,lambda uid:{'id':uid,'username':record['username'],'enabled':True})
            self.assertEqual(create.call_count,1)

    def test_invalid_manifest_records_and_creation_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';valid=pop.initialize(root,1)
            for bad in ([],{},dict(valid,count=0),dict(valid,prefix='wrong'),dict(valid,prefix='perf-'+'g'*16)):
                pop.save(root,'population.json',bad)
                with self.assertRaises(ValueError):pop.run(root,Mock(),Mock())
            pop.save(root,'population.json',valid)
            base={'username':valid['prefix']+'-00001','password':'x'*32,'state':'prepared','user_id':None}
            for change in ({'password':'bad'}, {'state':'unknown'}, {'user_id':str(uuid.uuid4())},
                           {'state':'created','user_id':'invalid'}, {'state':'created','user_id':None}):
                pop.save(root,'00001.json',{**base,**change})
                with self.assertRaises(ValueError):pop.run(root,Mock(),Mock())
            for returned in (None,'not-a-uuid'):
                pop.save(root,'00001.json',base)
                with self.assertRaisesRegex(ValueError,'not confirmed'):pop.run(root,Mock(return_value=returned),Mock())
                self.assertEqual(pop.load(root,'00001.json')['state'],'creating')

    def test_private_record_size_and_exclusive_population_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';pop.initialize(root,1)
            p=root/'large';p.write_bytes(b'x'*65537);p.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'oversized'):pop.load(root,'large')
            p.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'private'):pop.load(root,'large')
            alias=root/'alias';alias.symlink_to(p)
            with self.assertRaises(OSError):pop.load(root,'alias')
            fd=os.open(root/'.lock',os.O_CREAT|os.O_WRONLY,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(BlockingIOError):pop.run(root,Mock(),Mock())
            finally:os.close(fd)

    def test_export_contains_only_identity_profiles_not_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users';manifest=pop.initialize(root,2);records=[]
            for index in (1,2):
                record={'username':manifest['prefix']+'-'+str(index).zfill(5),'password':'private-sentinel-'+'x'*32,
                        'state':'created','user_id':str(uuid.uuid4())}
                pop.save(root,str(index).zfill(5)+'.json',record);records.append(record)
            exported=pop.export_profiles(root)
            self.assertEqual(exported['population'],manifest['prefix'])
            self.assertEqual(len(exported['profiles']),2)
            self.assertNotIn('private-sentinel',repr(exported))
            self.assertNotIn('password',repr(exported))
            for row,record in zip(exported['profiles'],records):
                self.assertEqual(set(row),{'user_id','tags'})
                self.assertEqual(row['user_id'],record['user_id'])
                self.assertEqual(row['tags']['email'],record['username']+'@example.test')
            for change in ({'state':'creating'},{'user_id':records[0]['user_id']},{'user_id':'bad'},
                           {'username':'foreign'}, {'user_id':None}):
                pop.save(root,'00002.json',{**records[1],**change})
                with self.assertRaises(ValueError):pop.export_profiles(root)

    def test_bounds_existing_directory_and_foreign_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'users'
            for count in (0,10001,True):
                with self.assertRaises(ValueError):pop.initialize(root,count)
            pop.initialize(root,1)
            with self.assertRaises(FileExistsError):pop.initialize(root,1)
            pop.save(root,'00001.json',{'username':'foreign','password':'x'*32,'state':'prepared','user_id':None})
            with self.assertRaises(ValueError):pop.run(root,Mock(),Mock())
            root.chmod(0o755)
            with self.assertRaises(ValueError):pop.load(root,'population.json')


if __name__=='__main__':unittest.main()
