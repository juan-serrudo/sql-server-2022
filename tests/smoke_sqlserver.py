#!/usr/bin/env python3
"""Prueba real no destructiva: COPY_ONLY de model, metadatos y VERIFYONLY; nunca RESTORE DATABASE."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('historico', ROOT / 'scripts/historico.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def main():
    os.umask(0o077)
    sql = h.SQL()
    info = h.server_info(sql)
    filename = '/tmp/historico-smoke-' + uuid.uuid4().hex + '.bak'
    results = {'at': h.now(), 'server': info, 'restored_databases': 0, 'checks': []}
    try:
        sql.query(f'BACKUP DATABASE [model] TO DISK = {h.literal(filename)} WITH COPY_ONLY, CHECKSUM;')
        results['checks'].append('COPY_ONLY de model a archivo temporal único')
        encoded = h.literal(filename).replace("'", "''")
        headers = sql.query((ROOT / 'sql/header.sql').read_text().replace('@@DISK_EXEC@@', encoded), as_json=True)
        if len(headers) != 1 or headers[0]['DatabaseName'] != 'model' or headers[0]['BackupType'] != 1:
            raise h.Failure('Encabezado de prueba inesperado.')
        results['checks'].append('HEADERONLY: esquema SQL 2022 de 59 columnas y JSON válido')
        position = headers[0]['Position']
        files = sql.query((ROOT / 'sql/filelist.sql').read_text().replace(
            '@@DISK_EXEC@@', encoded).replace('@@POSITION@@', str(position)), as_json=True)
        if not files or {row['Type'] for row in files} != {'D', 'L'}:
            raise h.Failure('FILELISTONLY de prueba inesperado.')
        results['checks'].append('FILELISTONLY: archivos de datos y log, nombres lógicos y tamaños reales')
        clauses, _ = h.move_clauses({'files': files}, 'smoke_' + uuid.uuid4().hex)
        sql.query(f'RESTORE VERIFYONLY FROM DISK = {h.literal(filename)} WITH FILE = {position}, '
                  + ', '.join(clauses) + ', CHECKSUM, STOP_ON_ERROR, STATS = 10;', progress=True)
        results['checks'].append('VERIFYONLY con FILE, todos los MOVE y CHECKSUM')
        sql.query('DBCC CHECKDB ([model]) WITH NO_INFOMSGS, ALL_ERRORMSGS;', quiet_errors=True)
        results['checks'].append('DBCC CHECKDB sin reparaciones en model')
        results['status'] = 'OK'
    except Exception as exc:
        results['status'], results['error'] = 'ERROR', h.clean(exc)
        raise
    finally:
        # Solo el archivo temporal creado por esta prueba; nunca respaldos de backups/ ni volúmenes.
        result = h.compose('exec', '-T', 'sqlserver', 'rm', '--', filename, check=False)
        results['temporary_file_removed'] = result.returncode == 0
        target = ROOT / 'reports/prueba-sqlserver.json'
        target.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
        target.chmod(0o600)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR: ' + h.clean(exc), file=sys.stderr)
        sys.exit(1)
