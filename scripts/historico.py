#!/usr/bin/env python3
"""Administración local. Python estándar; SQL se ejecuta con sqlcmd ODBC 18."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / 'reports'
STATE = REPORTS / 'state.json'
EXPECTED = ('fiscalia2015', 'fiscalia2016', 'fiscalia2017')
SYSTEM = {'master', 'model', 'msdb', 'tempdb', 'distribution'}
SECRET = ''


class Failure(Exception):
    pass


def now():
    return dt.datetime.now(ZoneInfo('America/La_Paz')).isoformat(timespec='seconds')


def clean(text):
    text = str(text)
    if SECRET:
        text = text.replace(SECRET, '[SECRETO OMITIDO]')
    return text


def run(args, *, data=None, env=None, timeout=60, check=True):
    try:
        p = subprocess.run(args, input=data, text=True, capture_output=True,
                           cwd=ROOT, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(clean(str(exc))) from exc
    if check and p.returncode:
        raise Failure(clean((p.stderr or p.stdout).strip() or f'Código {p.returncode}'))
    return p


def load_env():
    global SECRET
    path = ROOT / '.env'
    if not path.is_file():
        raise Failure('Falta .env. Copie .env.example, configure un secreto único y chmod 600 .env.')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise Failure('Permisos inseguros en .env. Ejecute: chmod 600 .env')
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r"(ACCEPT_EULA|MSSQL_SA_PASSWORD)=(?:'([^']*)'|([^\s'\"#$]+))", line)
        if not match or match[1] in values:
            raise Failure('.env inválido: use únicamente las dos claves de .env.example, sin interpolación.')
        values[match[1]] = match[2] if match[2] is not None else match[3]
    password = values.get('MSSQL_SA_PASSWORD', '')
    if (len(password) < 16 or len(password) > 128 or 'CAMBIAR' in password
            or sum(bool(re.search(pattern, password)) for pattern in
                   (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^a-zA-Z0-9]')) < 3):
        raise Failure('Contraseña inválida: 16–128 caracteres, al menos tres clases; sin valores de ejemplo.')
    if values.get('ACCEPT_EULA') != 'Y':
        raise Failure('Debe aceptar la licencia mediante ACCEPT_EULA=Y en .env.')
    SECRET = password
    # Impedir que el entorno del shell sustituya las credenciales de .env en Compose.
    env = os.environ.copy()
    for key in ('MSSQL_SA_PASSWORD', 'ACCEPT_EULA', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME',
                'COMPOSE_PROFILES', 'COMPOSE_ENV_FILES', 'SQLCMDPASSWORD'):
        env.pop(key, None)
    return env


def compose(*args, check=True, timeout=60, data=None, password=False):
    env = load_env()
    if password:
        env['SQLCMDPASSWORD'] = SECRET
    return run(['docker', 'compose', '--project-directory', str(ROOT),
                '--env-file', str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'),
                *args], env=env, data=data, check=check, timeout=timeout)


def save(state):
    REPORTS.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(STATE)


def read_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {'backups': {}, 'events': []}


def event(state, stage, status, detail):
    state.setdefault('events', []).append({'at': now(), 'stage': stage, 'status': status,
                                           'detail': clean(detail)})
    save(state)


def fingerprint(path):
    digest = hashlib.sha256()
    before = path.stat()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise Failure(f'{path.name}: archivo cambió durante la lectura; detenga cualquier copia en curso.')
    return {'sha256': digest.hexdigest(), 'bytes': after.st_size}


def backups(required=True):
    files = sorted(p for p in (ROOT / 'backups').iterdir() if p.suffix.lower() == '.bak')
    names = set()
    for p in files:
        if p.is_symlink() or not p.is_file() or not os.access(p, os.R_OK):
            raise Failure(f'{p.name}: se exige un archivo regular legible, sin enlaces simbólicos.')
        if any(ord(c) < 32 or ord(c) == 127 for c in p.name) or len(p.name) > 180:
            raise Failure('Nombre de respaldo inválido: evite controles y nombres mayores a 180 caracteres.')
        if p.name.casefold() in names:
            raise Failure('Respaldos con nombres ambiguos por mayúsculas/minúsculas.')
        names.add(p.name.casefold())
    if required and not files:
        raise Failure('No hay archivos .bak en backups/. Copie allí los respaldos originales, sin modificarlos.')
    return files


def literal(value):
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Failure('Metadatos con caracteres de control: requieren revisión manual.')
    return "N'" + value.replace("'", "''") + "'"


def identifier(value):
    return '[' + value.replace(']', ']]') + ']'


def disk(path):
    return literal('/var/opt/mssql/backup/' + path.name)


def json_sql(query):
    return "SELECT N'__JSON_BEGIN__';\n" + query + ";\nSELECT N'__JSON_END__';"


def parse_json(output):
    try:
        if output.count('__JSON_BEGIN__') != 1 or output.count('__JSON_END__') != 1:
            raise ValueError('Marcadores incompletos o ambiguos')
        section = output.split('__JSON_BEGIN__', 1)[1].split('__JSON_END__', 1)[0]
        payload = ''.join(section.splitlines()).strip()
        # sqlcmd ODBC no emite una fila FOR JSON cuando la consulta no tiene filas.
        result = json.loads(payload) if payload else []
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise ValueError('Se esperaba una lista de objetos')
        return result
    except (IndexError, ValueError) as exc:
        raise Failure('Respuesta JSON SQL incompleta o inesperada; no se utilizarán metadatos ambiguos.') from exc


class SQL:
    def __init__(self):
        probe = compose('exec', '-T', 'sqlserver', 'test', '-x',
                        '/opt/mssql-tools18/bin/sqlcmd', check=False)
        self.local = probe.returncode == 0
        if not self.local:
            image = compose('--profile', 'tools', 'images', '-q', 'tools')
            if not image.stdout.strip():
                raise Failure('sqlcmd ODBC 18 no está en el motor. Ejecute make tools y vuelva a intentar.')

    def command(self):
        if self.local:
            return ['exec', '-T', '-e', 'SQLCMDPASSWORD', 'sqlserver',
                    '/opt/mssql-tools18/bin/sqlcmd', '-S', 'localhost,1433']
        return ['--profile', 'tools', 'run', '--rm', '-T', '--no-deps',
                'tools', '-S', 'sqlserver,1433']

    def query(self, query, *, as_json=False, progress=False, quiet_errors=False):
        args = self.command() + ['-U', 'sa', '-N', '-C', '-b', '-V', '11', '-r', '1',
                                 '-l', '5', '-t', '0', '-x', '-X1', '-w', '65535', '-y', '0']
        sql = 'SET NOCOUNT ON;\n' + query + '\nGO\n'
        if progress:
            env = load_env()
            env['SQLCMDPASSWORD'] = SECRET
            command = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file',
                       str(ROOT / '.env'), '-f', str(ROOT / 'compose.yaml'), *args]
            try:
                with subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1) as proc:
                    proc.stdin.write(sql)
                    proc.stdin.close()
                    lines = []
                    for line in proc.stdout:
                        line = clean(line)
                        lines.append(line)
                        print(line, end='', flush=True)
                    code = proc.wait()
            except OSError as exc:
                raise Failure(clean(str(exc))) from exc
            if code:
                raise Failure(''.join(lines)[-12000:] or f'sqlcmd terminó con código {code}')
            return ''.join(lines)
        result = compose(*args, data=sql, check=False, timeout=None, password=True)
        if result.returncode:
            text = clean(result.stderr + result.stdout)
            if quiet_errors:
                codes = sorted(set(re.findall(r'(?:Msg|Mensaje)\s+(\d+)', text)))
                raise Failure(f'sqlcmd código {result.returncode}; mensajes SQL: {", ".join(codes) or "sin número"}. '
                              'Revisar DBCC localmente; se omiten detalles que podrían contener datos.')
            raise Failure(text[-12000:] or f'sqlcmd código {result.returncode}')
        if result.stderr.strip() and not quiet_errors:
            print(clean(result.stderr.strip()), file=sys.stderr)
        return parse_json(result.stdout) if as_json else result.stdout


def server_info(sql):
    rows = sql.query(json_sql("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)) AS version, "
                     "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition, "
                     "CAST(SERVERPROPERTY('ProductMajorVersion') AS int) AS major "
                     "FOR JSON PATH"), as_json=True)
    if len(rows) != 1 or rows[0]['major'] != 16 or 'Developer' not in rows[0]['edition']:
        raise Failure('Se requiere exclusivamente SQL Server 2022 Developer (versión mayor 16).')
    return rows[0]


def requirements(state):
    issues, warnings = [], []
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    memory = {line.split(':')[0]: int(line.split()[1]) * 1024
              for line in Path('/proc/meminfo').read_text().splitlines()}
    fs = shutil.disk_usage(ROOT)
    info = {'at': now(), 'distribution': release.get('PRETTY_NAME', '').strip('"'),
            'ubuntu_base': release.get('UBUNTU_CODENAME', '').strip('"'),
            'architecture': platform.machine(), 'cpu': os.cpu_count(),
            'memory_total': memory['MemTotal'], 'memory_available': memory['MemAvailable'],
            'disk_project_free': fs.free, 'disk_project_total': fs.total}
    if info['architecture'] != 'x86_64' or platform.system() != 'Linux':
        issues.append('Se requiere Linux x86_64 nativo; no se usará emulación ARM.')
    if info['cpu'] < 2:
        issues.append('Se requieren al menos 2 CPU lógicas.')
    if memory['MemTotal'] < 2 * 1024**3 or memory['MemAvailable'] < 2 * 1024**3:
        issues.append('Se necesitan al menos 2 GiB de RAM total y disponible.')
    elif memory['MemAvailable'] < 6 * 1024**3:
        warnings.append('Recomendados 6 GiB disponibles para el límite del motor de 4 GiB más el sistema.')
    if fs.free < 6 * 1024**3:
        issues.append('Menos de 6 GiB libres para imágenes y archivos de trabajo.')
    try:
        files = backups(False)
        info['backups'] = [{'file': p.name, **fingerprint(p)} for p in files]
        if not files:
            warnings.append('Sin respaldos: copie fiscalia2015.bak, fiscalia2016.bak y fiscalia2017.bak en backups/.')
    except Failure as exc:
        issues.append(str(exc))
    docker_info = None
    for key, command in [('docker_client', ['docker', '--version']),
                         ('compose', ['docker', 'compose', 'version'])]:
        try:
            info[key] = run(command).stdout.strip()
        except Failure:
            issues.append(f'{key} no disponible. Consulte README.md para instalación oficial en Mint 22.')
    try:
        docker_info = json.loads(run(['docker', 'info', '--format', '{{json .}}']).stdout)
        if not docker_info.get('ServerVersion'):
            raise Failure('Docker no devolvió versión del servicio.')
        info['docker_server'] = docker_info['ServerVersion']
        info['docker_architecture'] = docker_info['Architecture']
        info['docker_memory'] = docker_info['MemTotal']
        info['docker_cpu'] = docker_info['NCPU']
        info['docker_access'] = 'OK; servicio accesible con el usuario actual'
        service = run(['systemctl', 'is-active', 'docker'], check=False)
        info['docker_systemd_service'] = service.stdout.strip() or 'No consultable; daemon Docker accesible'
        if docker_info['Architecture'] not in ('x86_64', 'amd64'):
            issues.append('El daemon Docker no tiene arquitectura x86_64.')
        if docker_info['NCPU'] < 2 or docker_info['MemTotal'] < 2 * 1024**3:
            issues.append('El daemon Docker dispone de menos de 2 CPU o 2 GiB RAM.')
        docker_root = Path(docker_info.get('DockerRootDir', '/var/lib/docker'))
        try:
            info['disk_docker_free'] = shutil.disk_usage(docker_root).free
            if info['disk_docker_free'] < 6 * 1024**3:
                issues.append('Menos de 6 GiB libres en el almacenamiento Docker.')
        except OSError:
            warnings.append('Espacio del directorio Docker no accesible; se comprobará dentro del contenedor.')
    except (Failure, ValueError, KeyError) as exc:
        issues.append('Docker inaccesible: servicio detenido o permisos insuficientes. ' + clean(str(exc)))
    # Leer /proc evita depender de netlink/ss y no reserva el puerto del usuario.
    port_used = False
    try:
        for path in ('/proc/net/tcp', '/proc/net/tcp6'):
            for line in Path(path).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].split(':')[1], 16) == 1433:
                    port_used = True
        info['port_1433'] = 'ocupado' if port_used else 'libre'
        if port_used:
            own = False
            try:
                ids = compose('ps', '-q', 'sqlserver').stdout.strip()
                if ids:
                    inspect = json.loads(run(['docker', 'inspect', ids]).stdout)[0]
                    own = bool(inspect['State']['Running']) and any(
                        p.get('HostIp') == '127.0.0.1' and p.get('HostPort') == '1433'
                        for p in (inspect['NetworkSettings']['Ports'].get('1433/tcp') or []))
            except (Failure, ValueError, KeyError):
                pass
            if not own:
                issues.append('TCP 1433 ya está ocupado por otro servicio; no se intentará detenerlo.')
    except OSError:
        issues.append('No se pudo comprobar el puerto 1433 en /proc/net/tcp*.')
    info['issues'], info['warnings'] = issues, warnings
    state['host'] = info
    event(state, 'check', 'ERROR' if issues else 'OK', '; '.join(issues + warnings) or 'Requisitos cumplidos')
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if issues:
        raise Failure('Requisitos pendientes. Consulte los detalles anteriores y README.md.')


def wait(sql=None, seconds=300):
    sql = sql or SQL()
    end = time.monotonic() + seconds
    last = ''
    while time.monotonic() < end:
        try:
            info = server_info(sql)
            print('SQL Server 2022 Developer disponible mediante conexión SQL cifrada.')
            return sql, info
        except Failure as exc:
            last = str(exc)
            time.sleep(3)
    raise Failure(f'SQL Server no estuvo disponible en {seconds}s: {last}')


def readable(path):
    result = compose('exec', '-T', 'sqlserver', 'test', '-r',
                     '/var/opt/mssql/backup/' + path.name, check=False)
    if result.returncode:
        raise Failure(f'{path.name}: SQL Server no puede leer el montaje. Consulte permisos ACL en README.md.')


def inspect_backups(state, sql):
    failures = []
    for path in backups():
        print(f'Inspeccionando {path.name}...', flush=True)
        record = {'at': now(), 'file': path.name}
        state['backups'][path.name] = record  # Invalida verificaciones de inspecciones anteriores.
        try:
            record.update(fingerprint(path))
            readable(path)
            query = (ROOT / 'sql/header.sql').read_text().replace('@@DISK_EXEC@@', disk(path).replace("'", "''"))
            headers = sql.query(query, as_json=True)
            if not headers:
                raise Failure('HEADERONLY no devolvió conjuntos de respaldo.')
            record['sets'] = []
            positions = set()
            for header in headers:
                item = {'header': header}
                record['sets'].append(item)
                position = header.get('Position')
                if not isinstance(position, int) or position < 1 or position in positions:
                    item['inspection'] = 'ERROR'
                    item['error'] = 'Posición no válida o protegida por contraseña; requiere revisión manual.'
                    failures.append(f'{path.name}: {item["error"]}')
                    continue
                positions.add(position)
                try:
                    query = (ROOT / 'sql/filelist.sql').read_text().replace(
                        '@@DISK_EXEC@@', disk(path).replace("'", "''")).replace('@@POSITION@@', str(position))
                    item['files'] = sql.query(query, as_json=True)
                    if not item['files']:
                        raise Failure('FILELISTONLY no devolvió archivos.')
                    item['inspection'] = 'OK'
                except Failure as exc:
                    item['inspection'], item['error'] = 'ERROR', clean(exc)
                    failures.append(f'{path.name}, FILE={position}: {exc}')
            if fingerprint(path)['sha256'] != record['sha256']:
                raise Failure('Respaldo cambió durante la inspección; se invalidan los metadatos.')
            record['inspection'] = 'OK' if all(s['inspection'] == 'OK' for s in record['sets']) else 'PARCIAL'
            print(f'  {len(headers)} conjunto(s); detalle en make report.')
        except Failure as exc:
            record['inspection'], record['error'] = 'ERROR', clean(exc)
            failures.append(f'{path.name}: {exc}')
        save(state)
    event(state, 'inspect', 'ERROR' if failures else 'OK', '\n'.join(failures) or 'Todos los respaldos inspeccionados')
    if failures:
        raise Failure('\n'.join(failures))


def current_record(state, path):
    record = state['backups'].get(path.name, {})
    if not record.get('sets') or record.get('inspection') not in ('OK', 'PARCIAL'):
        raise Failure(f'{path.name}: primero ejecute make inspect.')
    if fingerprint(path)['sha256'] != record.get('sha256'):
        raise Failure(f'{path.name}: SHA-256 cambió. Repita make inspect y make verify.')
    return record


def incompatibilities(item):
    h, files = item['header'], item.get('files', [])
    reasons = []
    if item.get('inspection') != 'OK':
        reasons.append('Inspección de archivos incompleta; revisar claves, certificados o medios adicionales.')
    if h.get('BackupType') != 1:
        reasons.append('No es un respaldo completo: requiere una cadena de recuperación y respaldos adicionales.')
    if (h.get('DatabaseName') or '').casefold() in SYSTEM:
        reasons.append('Base de sistema no admitida para restauración histórica independiente.')
    if h.get('SoftwareVersionMajor') is None or not 9 <= h['SoftwareVersionMajor'] <= 16:
        reasons.append('Versión de origen no compatible con esta ruta de restauración.')
    for flag in ('IsDamaged', 'IsSnapshot', 'HasIncompleteMetaData'):
        if h.get(flag):
            reasons.append(f'{flag}=1; requiere recuperación especializada, no se forzará.')
    if h.get('EncryptorThumbprint') or h.get('KeyAlgorithm') not in (None, 'NO_Encryption'):
        reasons.append('Respaldo cifrado: requiere certificado/clave privada y revisión DBA explícita.')
    if any(f.get('TDEThumbprint') for f in files):
        reasons.append('TDE: requiere certificado y clave privada originales en master; revisar con DBA.')
    if any(f.get('Type') not in ('D', 'L') or f.get('SnapshotURL') for f in files):
        reasons.append('Contiene FILESTREAM/Full-Text/snapshot u otro archivo especial; ruta automática no admitida.')
    if any(not f.get('IsPresent') for f in files):
        reasons.append('Archivos ausentes: requiere respaldos adicionales.')
    return reasons


def move_clauses(item, target):
    files = item.get('files', [])
    if not files or {f['Type'] for f in files} != {'D', 'L'}:
        raise Failure('Se requiere al menos un archivo de datos D y uno de log L; tipos especiales no admitidos.')
    logical, ids, clauses, paths = set(), set(), [], []
    for f in files:
        name, file_id = f['LogicalName'], f['FileID']
        if not name or name in logical or not isinstance(file_id, int) or file_id < 1 or file_id in ids:
            raise Failure('Nombres lógicos o FileID ambiguos; se requiere revisión manual.')
        logical.add(name)
        ids.add(file_id)
        extension = 'ldf' if f['Type'] == 'L' else ('mdf' if file_id == 1 else 'ndf')
        dest = f'/var/opt/mssql/data/{target}_{file_id}.{extension}'
        clauses.append(f'MOVE {literal(name)} TO {literal(dest)}')
        paths.append(dest)
    return clauses, paths


def classify_error(message):
    if re.search(r'33111|33112|33104|certificate|certificado|asymmetric|asimétrica|decrypt', message, re.I):
        return 'Requiere certificados/claves originales o revisión del cifrado.'
    if re.search(r'3132|3241|media famil|familia|additional|adicional', message, re.I):
        return 'Medio incompleto/no válido; puede necesitar otros archivos del mismo respaldo.'
    if re.search(r'3169|version|versión|incompatible', message, re.I):
        return 'Versión o característica incompatible; no se forzará.'
    return 'Error de lectura, permisos, espacio o integridad; revisar diagnóstico SQL.'


def verify_backups(state, sql):
    failures = []
    for path in backups():
        try:
            record = current_record(state, path)
            readable(path)
        except Failure as exc:
            failures.append(str(exc))
            continue
        for item in record['sets']:
            position = item['header'].get('Position')
            result = {'at': now(), 'sha256': record['sha256'], 'status': 'ERROR',
                      'limitations': incompatibilities(item)}
            item['verification'] = result
            try:
                if not isinstance(position, int) or position < 1:
                    raise Failure('Posición protegida o inválida; no se seleccionará arbitrariamente.')
                clauses = []
                if item.get('inspection') == 'OK' and item.get('files') and all(
                        f['Type'] in ('D', 'L') for f in item['files']):
                    # Rutas Linux separadas de las de restauración. VERIFYONLY no crea archivos.
                    clauses, _ = move_clauses(item, 'verify_' + record['sha256'][:16] + '_' + str(position))
                checksum = ['CHECKSUM'] if item['header'].get('HasBackupChecksums') else []
                options = [f'FILE = {position}', *clauses, *checksum, 'STOP_ON_ERROR', 'STATS = 10']
                print(f'Verificando {path.name}, FILE={position}...', flush=True)
                sql.query(f'RESTORE VERIFYONLY FROM DISK = {disk(path)} WITH ' + ', '.join(options) + ';', progress=True)
                if fingerprint(path)['sha256'] != record['sha256']:
                    raise Failure('SHA-256 cambió durante VERIFYONLY; resultado inválido.')
                result['status'] = 'OK'
                result['checksum'] = bool(checksum)
                print('  Legible y verificable; validación completa pendiente de restauración y CHECKDB.')
            except Failure as exc:
                result['error'] = clean(exc)
                result['diagnosis'] = classify_error(str(exc))
                failures.append(f'{path.name}, FILE={position}: {result["diagnosis"]} {exc}')
            save(state)
    event(state, 'verify', 'ERROR' if failures else 'OK', '\n'.join(failures) or 'VERIFYONLY completado; no garantiza integridad absoluta')
    if failures:
        raise Failure('\n'.join(failures))


def selections(state, files):
    plan = ROOT / 'restore-plan.json'
    if plan.exists():
        try:
            data = json.loads(plan.read_text())
            selected = data['selections']
            if set(data) != {'selections'} or not isinstance(selected, list):
                raise ValueError('estructura')
        except (ValueError, KeyError, TypeError) as exc:
            raise Failure('restore-plan.json inválido; revise restore-plan.json.example.') from exc
    else:
        selected = []
        for path in files:
            record = current_record(state, path)
            if len(record['sets']) != 1 or path.stem.lower() not in EXPECTED:
                raise Failure(f'{path.name}: varios conjuntos o nombre desconocido. Cree restore-plan.json '
                              'seleccionando explícitamente FILE y destino según make inspect / make report.')
            selected.append({'file': path.name, 'position': record['sets'][0]['header']['Position'],
                             'target': path.stem.lower()})
    if not 1 <= len(selected) <= 3:
        raise Failure('Seleccione entre uno y tres conjuntos de respaldo.')
    available, targets, sources = {p.name: p for p in files}, set(), set()
    result = []
    for selection in selected:
        if not isinstance(selection, dict) or set(selection) != {'file', 'position', 'target'}:
            raise Failure('Cada selección debe contener únicamente file, position y target.')
        filename, position, target = selection['file'], selection['position'], selection['target']
        if not isinstance(filename, str) or filename not in available:
            raise Failure('Archivo seleccionado inexistente en backups/.')
        if (not isinstance(target, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', target)
                or target.casefold() in SYSTEM or target.casefold() in targets):
            raise Failure('Destino inválido, reservado o duplicado; use letras, números y guion bajo (máximo 64).')
        if type(position) is not int or position < 1 or (filename, position) in sources:
            raise Failure('Posición inválida o conjunto repetido en restore-plan.json.')
        record = current_record(state, available[filename])
        items = [s for s in record['sets'] if s['header'].get('Position') == position]
        if len(items) != 1:
            raise Failure(f'{filename}: posición {position} no inspeccionada o ambigua.')
        item = items[0]
        verification = item.get('verification', {})
        if verification.get('status') != 'OK' or verification.get('sha256') != record['sha256']:
            raise Failure(f'{filename}, FILE={position}: primero ejecute make verify con resultado OK.')
        reasons = incompatibilities(item)
        if reasons:
            raise Failure(f'{filename}, FILE={position}: ' + ' '.join(reasons))
        targets.add(target.casefold())
        sources.add((filename, position))
        result.append((available[filename], position, target, item, record['sha256']))
    return result


def restore_sql(path, position, target, item):
    clauses, _ = move_clauses(item, target)
    checksum = ['CHECKSUM'] if item['header'].get('HasBackupChecksums') else []
    options = [f'FILE = {position}', *clauses, *checksum, 'STOP_ON_ERROR', 'RECOVERY', 'STATS = 5']
    return (f"USE [master];\nIF DB_ID({literal(target)}) IS NOT NULL\n"
            "    THROW 51000, 'El destino ya existe. Se requiere una decisión explícita; no se sobrescribe.', 1;\n"
            f'RESTORE DATABASE {identifier(target)} FROM DISK = {disk(path)}\nWITH '
            + ',\n     '.join(options) + ';\n')


def restore_backups(state, sql):
    selected = selections(state, backups())  # Validar todo el plan antes de restaurar la primera base.
    state.setdefault('restorations', [])
    failures = []
    # Comprobación global: no dejar un lote parcialmente restaurado por una colisión conocida.
    existing = sql.query(json_sql('SELECT name FROM sys.databases FOR JSON PATH'), as_json=True)
    existing = {row['name'].casefold() for row in existing}
    collisions = [target for _, _, target, _, _ in selected if target.casefold() in existing]
    if collisions:
        raise Failure('Ya existen destinos: ' + ', '.join(collisions) + '. Lote detenido. '
                      'Decida explícitamente otro destino en restore-plan.json. No se eliminó ni sobrescribió nada.')
    # Espacio del volumen real, no del directorio del proyecto ni tamaño comprimido.
    free_output = compose('exec', '-T', 'sqlserver', 'df', '-Pk', '/var/opt/mssql').stdout
    try:
        available = int(free_output.splitlines()[-1].split()[3]) * 1024
    except (ValueError, IndexError) as exc:
        raise Failure('No se pudo determinar el espacio libre del volumen de SQL Server.') from exc
    needed = sum(int(f['Size']) for _, _, _, item, _ in selected for f in item['files'])
    if available < needed * 1.2 + 1024**3:
        raise Failure('Espacio insuficiente en volumen SQL: se exige tamaño original + 20% + 1 GiB.')
    for path, position, target, item, digest in selected:
        operation = {'at': now(), 'file': path.name, 'position': position, 'target': target,
                     'sha256': digest, 'status': 'INICIADA'}
        state['restorations'].append(operation)
        save(state)  # Una interrupción queda registrada; nunca se considera éxito por defecto.
        try:
            if fingerprint(path)['sha256'] != digest:
                raise Failure('Respaldo modificado desde VERIFYONLY. Repita inspección y verificación.')
            readable(path)
            _, paths = move_clauses(item, target)
            for dest in paths:
                probe = compose('exec', '-T', 'sqlserver', 'test', '-e', dest, check=False)
                if probe.returncode == 0:
                    raise Failure(f'Archivo de destino ya existe: {dest}. Decida otro destino; no se sobrescribe.')
                if probe.returncode != 1:
                    raise Failure('No se pudo comprobar la ausencia de archivos de destino.')
            query = restore_sql(path, position, target, item)
            generated = ROOT / 'sql/generated'
            generated.mkdir(mode=0o700, exist_ok=True)
            script = generated / f'{target}.sql'
            script.write_text(query)
            script.chmod(0o600)
            print(f'Restaurando {path.name}, FILE={position} → {target}...', flush=True)
            sql.query(query, progress=True)
            if fingerprint(path)['sha256'] != digest:
                raise Failure('El respaldo cambió durante la restauración. Base creada, validación obligatoria.')
            operation['status'] = 'RESTAURADA; validación pendiente'
            print(f'{target}: restaurada. Ejecute make validate.')
        except Failure as exc:
            operation['status'], operation['error'] = 'ERROR', clean(exc)
            failures.append(f'{target}: {exc}')
            save(state)
            break  # Evitar continuar un lote tras un fallo real.
        save(state)
    event(state, 'restore', 'ERROR' if failures else 'OK', '\n'.join(failures) or 'Restauración secuencial completada; CHECKDB pendiente')
    if failures:
        raise Failure('\n'.join(failures))


def validate(state, sql):
    targets = list(EXPECTED)
    for op in state.get('restorations', []):
        if op['target'] not in targets:
            targets.append(op['target'])
    results, failures = [], []
    for target in targets:
        result = {'at': now(), 'name': target, 'status': 'ERROR'}
        results.append(result)
        try:
            rows = sql.query(json_sql('SELECT name, state_desc, compatibility_level FROM sys.databases '
                                      f'WHERE name={literal(target)} FOR JSON PATH'), as_json=True)
            if not rows:
                raise Failure('Base ausente; no fue restaurada.')
            result.update(rows[0])
            if rows[0]['state_desc'] != 'ONLINE':
                raise Failure('Base fuera de estado ONLINE; no se ejecutará CHECKDB.')
            db = identifier(target)
            result['files'] = sql.query(json_sql(
                f'SELECT file_id, type_desc, CONVERT(bigint,size)*8192 AS bytes FROM {db}.sys.database_files FOR JSON PATH'),
                as_json=True)
            result['counts'] = sql.query(json_sql(
                f'SELECT (SELECT COUNT_BIG(*) FROM {db}.sys.tables WHERE is_ms_shipped=0) AS tables, '
                f'(SELECT COUNT_BIG(*) FROM {db}.sys.views WHERE is_ms_shipped=0) AS views, '
                f'(SELECT COUNT_BIG(*) FROM {db}.sys.procedures WHERE is_ms_shipped=0) AS procedures FOR JSON PATH'),
                as_json=True)[0]
            print(f'{target}: ejecutando DBCC CHECKDB completo; puede tardar...', flush=True)
            # No registrar la salida cruda de DBCC: puede mostrar valores de páginas de datos.
            sql.query(f'DBCC CHECKDB ({literal(target)}) WITH NO_INFOMSGS, ALL_ERRORMSGS;', quiet_errors=True)
            result['checkdb'] = 'OK: CHECKDB completo sin errores reportados'
            result['status'] = 'OK'
            result['warnings'] = ['Formato interno actualizado por SQL Server 2022; no volver directamente a 2014.',
                                  'Nivel de compatibilidad conservado; revisar aplicaciones y objetos SQL por separado.']
        except Failure as exc:
            result['error'] = clean(exc)
            failures.append(f'{target}: {exc}')
        state['validation'] = results
        save(state)
    event(state, 'validate', 'ERROR' if failures else 'OK', '\n'.join(failures) or 'Todas las bases ONLINE y CHECKDB sin errores')
    if failures:
        raise Failure('\n'.join(failures))


def report_value(value):
    text = clean(value)
    text = re.sub(r'(?i)([\\/]Users[\\/]|/home/)[^\\/]+', r'\1[usuario]', text)
    return text.replace('`', '\\`').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def generate_report(state):
    lines = ['# Informe técnico de restauración', '', f'Generado: {now()} (America/La_Paz).', '',
             'Los estados reflejan la última ejecución registrada, no una consulta en vivo. '
             'No se consultaron filas de tablas de usuario.', '', '## Equipo y herramientas', '']
    host = state.get('host')
    if host:
        lines += ['| Dato | Valor |', '|---|---|']
        lines += [f'| {key} | {report_value(value)} |' for key, value in host.items()
                  if key not in ('backups', 'issues', 'warnings')]
        lines += ['', 'Observaciones:', ''] + ['- ' + report_value(x) for x in host.get('issues', []) + host.get('warnings', [])]
    else:
        lines.append('Comprobación del equipo pendiente: make check.')
    lines += ['', '## Motor SQL Server', '']
    if state.get('server'):
        lines += ['| Dato | Valor |', '|---|---|']
        lines += [f'| {key} | {report_value(value)} |' for key, value in state['server'].items()]
    else:
        lines.append('No consultado; ejecutar make up.')
    lines += ['', '## Respaldos encontrados', '']
    try:
        found = backups(False)
        for path in found:
            lines.append(f'- {report_value(path.name)}: {path.stat().st_size} bytes.')
        if not found:
            lines.append('No hay archivos .bak en backups/.')
    except Failure as exc:
        lines.append(report_value(exc))
    for filename, record in state['backups'].items():
        lines += ['', f'### {report_value(filename)}', '',
                  f'Inspección: {record.get("inspection", "pendiente")}; fecha: {record.get("at", "—")}.',
                  f'SHA-256: {record.get("sha256", "no disponible")}; archivo: {record.get("bytes", "—")} bytes.',
                  f'Conjuntos detectados: {len(record.get("sets", []))}.']
        if record.get('error'):
            lines += ['', report_value(record['error'])]
        for item in record.get('sets', []):
            header = item['header']
            lines += ['', f'#### Conjunto FILE={header.get("Position", "desconocido")}', '',
                      '| Metadato | Valor |', '|---|---|']
            lines += [f'| {key} | {report_value(value)} |' for key, value in header.items()]
            lines += ['', '| Nombre lógico | Ruta original | Tipo | FileID | Bytes originales | TDE |',
                      '|---|---|---|---|---|---|']
            for f in item.get('files', []):
                lines.append('| ' + ' | '.join(report_value(f.get(k)) for k in (
                    'LogicalName', 'PhysicalName', 'Type', 'FileID', 'Size', 'TDEThumbprint')) + ' |')
            total = sum(int(f['Size']) for f in item.get('files', []))
            lines += ['', f'Tamaño original asignado (datos + log): {total} bytes.',
                      f'FILELISTONLY: {item.get("inspection", "pendiente")}.']
            verification = item.get('verification')
            if verification:
                lines += ['', f'VERIFYONLY: **{verification["status"]}**, ejecutado {verification["at"]}.',
                          'Checksum del respaldo comprobado: ' + ('sí' if verification.get('checksum') else 'no / no disponible') + '.']
                for key in ('diagnosis', 'error'):
                    if verification.get(key):
                        lines.append(report_value(verification[key]))
                lines += ['- ' + report_value(x) for x in verification.get('limitations', [])]
            else:
                lines.append('VERIFYONLY: pendiente.')
            if item.get('error'):
                lines.append(report_value(item['error']))
            lines += ['- ' + report_value(x) for x in incompatibilities(item)]
    lines += ['', '## Restauraciones', '']
    if not state.get('restorations'):
        lines.append('No se han ejecutado restauraciones.')
    else:
        lines += ['| Fecha | Archivo | FILE | Destino | Estado |', '|---|---|---|---|---|']
        for op in state['restorations']:
            lines.append('| ' + ' | '.join(report_value(op[k]) for k in ('at', 'file', 'position', 'target', 'status')) + ' |')
            if op.get('error'):
                lines.append(f'\nError de {report_value(op["target"])}: {report_value(op["error"])}\n')
    lines += ['', '## Estado e integridad de bases', '']
    if not state.get('validation'):
        lines.append('Pendiente: make validate después de restaurar.')
    else:
        lines += ['| Base | Fecha de control | Estado SQL | Compatibilidad | Tablas | Vistas | Procedimientos | CHECKDB / resultado |',
                  '|---|---|---|---|---|---|---|---|']
        for result in state['validation']:
            counts = result.get('counts', {})
            row = [result['name'], result['at'], result.get('state_desc', 'ausente/no disponible'),
                   result.get('compatibility_level', '—'), counts.get('tables', '—'), counts.get('views', '—'),
                   counts.get('procedures', '—'), result.get('checkdb', result.get('error', 'pendiente'))]
            lines.append('| ' + ' | '.join(report_value(v) for v in row) + ' |')
        for result in state['validation']:
            if result.get('files'):
                lines += ['', f'Archivos de {report_value(result["name"])}:', '',
                          '| FileID | Tipo | Bytes |', '|---|---|---|']
                lines += ['| ' + ' | '.join(report_value(f[k]) for k in ('file_id', 'type_desc', 'bytes')) + ' |'
                          for f in result['files']]
            lines += ['- ' + report_value(w) for w in result.get('warnings', [])]
    if state.get('component_tests'):
        lines += ['', '## Pruebas de componentes', '', '| Comprobación | Resultado |', '|---|---|']
        lines += [f'| {key} | {report_value(value)} |' for key, value in state['component_tests'].items()]
    smoke = REPORTS / 'prueba-sqlserver.json'
    if smoke.exists():
        evidence = json.loads(smoke.read_text())
        lines += ['', '## Prueba técnica del motor (sin restauraciones)', '',
                  f'Fecha: {report_value(evidence.get("at"))}; estado: {report_value(evidence.get("status"))}.',
                  'La prueba usa exclusivamente una copia temporal COPY_ONLY de model; '
                  'no valida respaldos institucionales.',
                  f'Bases restauradas por esta prueba: {evidence.get("restored_databases", "—")}.', '']
        lines += ['- ' + report_value(check) for check in evidence.get('checks', [])]
        lines.append('Archivo temporal eliminado: ' + report_value(evidence.get('temporary_file_removed')))
        if evidence.get('error'):
            lines.append(report_value(evidence['error']))
    lines += ['', '## Registro de operaciones y errores', '',
              '| Fecha | Operación | Resultado | Detalle |', '|---|---|---|---|']
    lines += ['| ' + ' | '.join(report_value(e[k]) for k in ('at', 'stage', 'status', 'detail')) + ' |'
              for e in state.get('events', [])]
    lines += ['', '## Limitaciones y recomendaciones', '',
              '- VERIFYONLY comprueba lectura y estructura del respaldo parcialmente; no garantiza integridad lógica.',
              '- La validación completa requiere restauración y DBCC CHECKDB. No se realizan reparaciones automáticas.',
              '- Fechas, nombre y versión de origen deben comprobarse en HEADERONLY; el nombre del .bak no prueba su contenido.',
              '- Varios conjuntos requieren selección explícita. Cadenas diferencial/log, medios divididos, cifrado, TDE y archivos especiales requieren intervención DBA.',
              '- El hash identifica bytes y detecta cambios, pero no prueba autenticidad ni integridad lógica del original.',
              '- Conservar los originales y un respaldo independiente. No publicar reports/, backups/, .env ni el volumen.',
              '- Conservar versión y digest de imagen para reproducir el motor; 2022-latest puede cambiar.',
              '- SQL Server 2022 actualiza el formato interno: sus nuevos respaldos no restauran directamente en SQL Server 2014.', '']
    target = REPORTS / 'informe-restauracion.md'
    target.write_text('\n'.join(lines))
    target.chmod(0o600)
    print(f'Informe generado: {target}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'up', 'wait', 'status', 'inspect', 'verify',
                        'restore', 'validate', 'report', 'logs', 'down', 'config', 'tools', 'sql'])
    parser.add_argument('file', nargs='?')
    args = parser.parse_args()
    os.umask(0o077)
    REPORTS.mkdir(mode=0o700, exist_ok=True)
    # Serializar comandos de este proyecto, incluso entre procesos make independientes.
    with (REPORTS / '.operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Failure('Otra operación está en curso. Espere a que termine antes de volver a ejecutar.')
        state = read_state()
        try:
            if args.action == 'check':
                requirements(state)
            elif args.action == 'up':
                requirements(state)
                compose('config', '--quiet')
                print('Iniciando SQL Server; la primera descarga puede tardar varios minutos.', flush=True)
                output = compose('--progress', 'quiet', 'up', '-d', 'sqlserver', timeout=None)
                print(clean(output.stderr or output.stdout))
                sql, info = wait()
                state['server'] = {**info, 'at': now()}
                image_id = compose('images', '-q', 'sqlserver').stdout.strip()
                if image_id:
                    image_info = json.loads(run(['docker', 'image', 'inspect', image_id]).stdout)[0]
                    state['server']['image_id'] = image_info['Id']
                    state['server']['digests'] = image_info.get('RepoDigests', [])
                for path in backups(False):
                    readable(path)
                event(state, 'up', 'OK', 'Motor SQL disponible; no se restauró ninguna base')
            elif args.action == 'wait':
                _, info = wait()
                state['server'] = {**info, 'at': now()}
                save(state)
            elif args.action == 'config':
                compose('config', '--quiet')
                print('Compose válido. Se omite la salida expandida para proteger credenciales.')
            elif args.action == 'tools':
                print('Construyendo cliente oficial mssql-tools18; requiere red.', flush=True)
                p = compose('--profile', 'tools', 'build', 'tools', timeout=None)
                print(clean(p.stdout + p.stderr))
            elif args.action in ('status', 'logs', 'down'):
                command = {'status': ['ps', '-a'], 'logs': ['logs', '--no-color', '--tail', '150', 'sqlserver'],
                           'down': ['down', '--timeout', '120']}[args.action]
                p = compose(*command, timeout=180)
                print(clean(p.stdout + p.stderr))
                if args.action == 'down':
                    event(state, 'down', 'OK', 'Contenedores detenidos; volumen persistente conservado')
            elif args.action == 'report':
                generate_report(state)
            else:
                if args.action in ('inspect', 'verify', 'restore'):
                    backups()  # Fallar antes de conectar si faltan los originales.
                sql = SQL()
                state['server'] = {**state.get('server', {}), **server_info(sql), 'at': now()}
                save(state)
                if args.action == 'sql':
                    if not args.file:
                        raise Failure('Uso: make sql FILE=sql/consulta-ejemplo.sql')
                    query = Path(args.file).read_text()
                    print(sql.query(query))
                else:
                    {'inspect': inspect_backups, 'verify': verify_backups,
                     'restore': restore_backups, 'validate': validate}[args.action](state, sql)
        except Failure as exc:
            last = state.get('events', [])[-1] if state.get('events') else {}
            if not (last.get('stage') == args.action and last.get('status') == 'ERROR'
                    and last.get('detail') == clean(exc)):
                event(state, args.action, 'ERROR', exc)
            if args.action != 'report':
                generate_report(state)
            raise
        if args.action not in ('report', 'status', 'logs', 'config', 'sql'):
            generate_report(state)


if __name__ == '__main__':
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        print('ERROR: ' + clean(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Operación interrumpida. Revise make status / make report antes de continuar.', file=sys.stderr)
        sys.exit(130)
