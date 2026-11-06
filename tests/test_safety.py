"""Pruebas de barreras de restauración; no necesitan Docker ni modifican bases."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('historico', Path(__file__).resolve().parents[1] / 'scripts/historico.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()
        (self.root / 'reports').mkdir()
        self.patch = patch.multiple(h, ROOT=self.root, REPORTS=self.root / 'reports', STATE=self.root / 'reports/state.json')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.file = self.root / 'backups/fiscalia2015.bak'
        self.file.write_bytes(b'fixture: esto no es un respaldo SQL real')
        self.digest = h.fingerprint(self.file)['sha256']
        self.item = {
            'inspection': 'OK',
            'header': {'Position': 1, 'BackupType': 1, 'DatabaseName': 'historico',
                       'SoftwareVersionMajor': 12, 'HasBackupChecksums': True},
            'files': [
                {'LogicalName': "datos'originales", 'Type': 'D', 'FileID': 1, 'Size': 1024, 'IsPresent': True},
                {'LogicalName': 'datos_secundarios', 'Type': 'D', 'FileID': 3, 'Size': 1024, 'IsPresent': True},
                {'LogicalName': 'log_original', 'Type': 'L', 'FileID': 2, 'Size': 1024, 'IsPresent': True},
                {'LogicalName': 'log_secundario', 'Type': 'L', 'FileID': 4, 'Size': 1024, 'IsPresent': True},
            ],
            'verification': {'status': 'OK', 'sha256': self.digest},
        }
        self.state = {'backups': {self.file.name: {'sha256': self.digest, 'inspection': 'OK', 'sets': [self.item]}}, 'events': []}

    def test_single_known_complete_verified_set_can_be_selected(self):
        selected = h.selections(self.state, [self.file])
        self.assertEqual(selected[0][1:3], (1, 'fiscalia2015'))

    def test_hash_change_invalidates_inspection_and_verification(self):
        self.file.write_bytes(b'cambio posterior')
        with self.assertRaisesRegex(h.Failure, 'SHA-256'):
            h.selections(self.state, [self.file])

    def test_multiple_sets_require_explicit_choice_even_if_only_one_full(self):
        extra = copy.deepcopy(self.item)
        extra['header'].update(Position=2, BackupType=2)
        self.state['backups'][self.file.name]['sets'].append(extra)
        with self.assertRaisesRegex(h.Failure, 'varios conjuntos'):
            h.selections(self.state, [self.file])
        (self.root / 'restore-plan.json').write_text(json.dumps({'selections': [
            {'file': self.file.name, 'position': 1, 'target': 'elegida'}]}))
        self.assertEqual(h.selections(self.state, [self.file])[0][2], 'elegida')

    def test_unverified_or_different_hash_is_blocked(self):
        for verification in ({}, {'status': 'ERROR', 'sha256': self.digest}, {'status': 'OK', 'sha256': 'otro'}):
            with self.subTest(verification=verification):
                self.item['verification'] = verification
                with self.assertRaisesRegex(h.Failure, 'make verify'):
                    h.selections(self.state, [self.file])

    def test_differential_log_future_version_tde_snapshot_and_partial_are_blocked(self):
        mutations = [
            lambda i: i['header'].update(BackupType=5),
            lambda i: i['header'].update(BackupType=2),
            lambda i: i['header'].update(SoftwareVersionMajor=17),
            lambda i: i['header'].update(DatabaseName='master'),
            lambda i: i['header'].update(IsSnapshot=True),
            lambda i: i['header'].update(IsDamaged=True),
            lambda i: i['header'].update(EncryptorThumbprint='AQID'),
            lambda i: i['files'][0].update(TDEThumbprint='AQID'),
            lambda i: i['files'][0].update(Type='S'),
            lambda i: i['files'][0].update(IsPresent=False),
            lambda i: i.update(inspection='ERROR'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                item = copy.deepcopy(self.item)
                mutate(item)
                self.state['backups'][self.file.name]['sets'] = [item]
                with self.assertRaises(h.Failure):
                    h.selections(self.state, [self.file])

    def test_moves_all_data_and_logs_and_quotes_logical_names(self):
        query = h.restore_sql(self.file, 1, 'fiscalia2015', self.item)
        self.assertEqual(query.count('MOVE '), 4)
        self.assertIn("N'datos''originales'", query)
        self.assertIn('fiscalia2015_3.ndf', query)
        self.assertIn('fiscalia2015_4.ldf', query)
        self.assertIn('IF DB_ID(', query)
        self.assertNotIn('REPLACE', query)
        self.assertNotIn('DROP DATABASE', query)

    def test_collision_blocks_whole_batch_before_any_restore(self):
        class FakeSQL:
            def query(self, query, **kwargs):
                if 'sys.databases' in query:
                    return [{'name': 'FISCALIA2015'}]
                raise AssertionError('No debe ejecutar otra consulta antes de detener el lote')
        with self.assertRaisesRegex(h.Failure, 'Ya existen destinos'):
            h.restore_backups(self.state, FakeSQL())
        self.assertEqual(self.state['restorations'], [])

    def test_malicious_targets_and_duplicate_destinations_are_rejected(self):
        for target in ('master', '../afuera', 'x]; DROP DATABASE model;--', 'x\nGO', ''):
            with self.subTest(target=target):
                (self.root / 'restore-plan.json').write_text(json.dumps({'selections': [
                    {'file': self.file.name, 'position': 1, 'target': target}]}))
                with self.assertRaises(h.Failure):
                    h.selections(self.state, [self.file])

    def test_control_characters_and_duplicate_file_ids_are_rejected(self):
        self.item['files'][0]['LogicalName'] = 'x\nGO\n!! comando'
        with self.assertRaises(h.Failure):
            h.move_clauses(self.item, 'segura')
        self.item['files'][0]['LogicalName'] = 'datos'
        self.item['files'][1]['FileID'] = 1
        with self.assertRaises(h.Failure):
            h.move_clauses(self.item, 'segura')

    def test_split_json_preserves_escaped_unicode_and_paths(self):
        expected = [{'DatabaseName': 'histórico', 'PhysicalName': 'C:\\datos\\base.mdf'}]
        data = json.dumps(expected, ensure_ascii=False)
        output = '__JSON_BEGIN__\n' + data[:20] + '\n' + data[20:] + '\n__JSON_END__\n'
        self.assertEqual(h.parse_json(output), expected)
        with self.assertRaises(h.Failure):
            h.parse_json('sin marcadores')

    def test_empty_sql_result_is_empty_array_and_missing_end_is_rejected(self):
        self.assertEqual(h.parse_json('__JSON_BEGIN__\n__JSON_END__\n'), [])
        with self.assertRaises(h.Failure):
            h.parse_json('__JSON_BEGIN__\n[]\n')

    def test_symlink_not_read_as_backup(self):
        alias = self.root / 'backups/enlace.bak'
        alias.symlink_to(self.file)
        with self.assertRaisesRegex(h.Failure, 'enlaces simbólicos'):
            h.backups()

    def test_env_cannot_execute_shell_or_expand_dollar(self):
        secret = 'Aa9!$(echo peligro)#algo'
        path = self.root / '.env'
        path.write_text("ACCEPT_EULA=Y\nMSSQL_SA_PASSWORD='" + secret + "'\n")
        path.chmod(0o600)
        with patch.dict('os.environ', {'MSSQL_SA_PASSWORD': 'override'}):
            env = h.load_env()
        self.assertEqual(h.SECRET, secret)
        self.assertNotIn('MSSQL_SA_PASSWORD', env)
        self.assertNotIn(secret, h.clean('falló con ' + secret))
        path.chmod(0o644)
        with self.assertRaisesRegex(h.Failure, 'Permisos inseguros'):
            h.load_env()


if __name__ == '__main__':
    unittest.main()
